import re
import json
import requests
from flask import Flask, render_template, request, jsonify
import chess
import chess.engine
import chess.pgn
import io
import os

app = Flask(__name__)

LLM_API_URL = "http://10.93.24.194:42005"
LLM_API_KEY = "my-secret-api-key"  # Set your API key here if required

# Proxy for accessing chess.com from Russia
CHESSCOM_PROXIES = {
    "http": "http://cnwgjtmx:5swmqv9vloap@142.111.67.146:5611",
    "https": "http://cnwgjtmx:5swmqv9vloap@142.111.67.146:5611"
}


def parse_chesscom_url(url):
    """Extract game_type and game_id from chess.com URL."""
    pattern = r"chess\.com/game/(live|daily)/([a-zA-Z0-9]+)"
    match = re.search(pattern, url)
    if match:
        return match.group(1), match.group(2)
    return "live", None


def fetch_chesscom_game(game_id, game_type="live"):
    """
    Fetch game data from chess.com API.
    Returns (pgn_string, game_metadata) tuple.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    # Step 1: Fetch the game page to extract player info
    page_url = f"https://www.chess.com/game/{game_type}/{game_id}"
    response = requests.get(page_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
    
    if response.status_code != 200:
        raise Exception(f"Could not access game page (status {response.status_code}).")
    
    html_content = response.text
    
    # Extract player usernames from meta description
    vs_pattern = r'content="(\S+)\s*\(\d+\)\s*vs\s*(\S+)\s*\(\d+\)'
    match = re.search(vs_pattern, html_content)
    
    if not match:
        vs_pattern2 = r'<title>(\S+)\s+vs\s+(\S+)'
        match = re.search(vs_pattern2, html_content)
    
    if not match:
        raise Exception("Could not extract player names from the game page.")
    
    white_username = match.group(1).rstrip('.,;:!?')
    black_username = match.group(2).rstrip('.,;:!?')
    
    # Step 2: Search both players' archives for the full PGN
    for username in [white_username, black_username]:
        pgn = _search_archives(username, game_id, headers, game_type)
        if pgn and '[Event' in pgn and '1.' in pgn:
            # Extract metadata from PGN
            game_obj = chess.pgn.read_game(io.StringIO(pgn))
            if game_obj:
                metadata = {
                    "white": game_obj.headers.get("White", "?"),
                    "black": game_obj.headers.get("Black", "?"),
                    "result": game_obj.headers.get("Result", "?"),
                    "white_elo": game_obj.headers.get("WhiteElo", "?"),
                    "black_elo": game_obj.headers.get("BlackElo", "?"),
                    "eco": game_obj.headers.get("ECO", "?"),
                    "termination": game_obj.headers.get("Termination", "?"),
                    "time_control": game_obj.headers.get("TimeControl", "?"),
                    "date": game_obj.headers.get("Date", "?"),
                }
            else:
                metadata = {"white": white_username, "black": black_username, "result": "?"}
            return pgn, metadata
    
    # Step 3: Fallback to callback API (metadata only, no moves)
    callback_url = f"https://www.chess.com/callback/{game_type}/game/{game_id}"
    response = requests.get(callback_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
    
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, dict) and "game" in data:
                game_data = data["game"]
                pgn_headers = game_data.get("pgnHeaders", {})
                
                if pgn_headers:
                    pgn = _build_pgn_headers_only(pgn_headers, game_id, game_data)
                    metadata = {
                        "white": pgn_headers.get("White", "?"),
                        "black": pgn_headers.get("Black", "?"),
                        "result": pgn_headers.get("Result", "?"),
                        "white_elo": pgn_headers.get("WhiteElo", "?"),
                        "black_elo": pgn_headers.get("BlackElo", "?"),
                        "eco": pgn_headers.get("ECO", "?"),
                        "termination": game_data.get("resultMessage", "?"),
                        "time_control": pgn_headers.get("TimeControl", "?"),
                        "date": pgn_headers.get("Date", "?"),
                    }
                    return pgn, metadata
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    
    raise Exception(
        f"Could not find game {game_id}. Players: {white_username} vs {black_username}. "
        f"The game may be very recent - wait a few minutes and try again."
    )


def _build_pgn_headers_only(pgn_headers, game_id, game_data):
    """Build minimal PGN with just headers for fallback."""
    pgn_lines = []
    for key in ["Event", "Site", "Date", "White", "Black", "Result", "ECO",
                "WhiteElo", "BlackElo", "TimeControl", "Termination"]:
        val = pgn_headers.get(key, "?")
        pgn_lines.append(f'[{key} "{val}"]')
    pgn_lines.append('')
    pgn_lines.append(f"; Game ID: {game_id}")
    pgn_lines.append(f"; Result: {game_data.get('resultMessage', '?')}")
    pgn_lines.append(pgn_headers.get("Result", "*"))
    return '\n'.join(pgn_lines)


def _search_archives(username, game_id, headers, game_type="live"):
    """Search through a player's monthly archives."""
    from datetime import datetime
    
    now = datetime.now()
    # Search last 24 months to be safe
    for i in range(24):
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        
        archive_url = f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
        
        try:
            response = requests.get(archive_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
            if response.status_code == 200:
                data = response.json()
                for g in data.get("games", []):
                    if game_id in g.get("url", ""):
                        return g.get("pgn")
        except Exception:
            continue
    
    return None


def _analyze_with_llm(pgn, metadata):
    """Send the game to the Qwen LLM for commentary."""
    # Extract moves from PGN for the prompt
    moves_list = []
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if game:
            board = game.board()
            for move in game.mainline_moves():
                san = board.san(move)
                moves_list.append(san)
                board.push(move)
    except Exception:
        pass

    # Build the prompt - request structured per-move analysis
    moves_str = '\n'.join([f"Move {i+1}: {m}" for i, m in enumerate(moves_list)])
    
    prompt = f"""You are a chess grandmaster and coach. Analyze the following game move by move.

Game Info:
- White: {metadata.get('white', '?')} (ELO: {metadata.get('white_elo', '?')})
- Black: {metadata.get('black', '?')} (ELO: {metadata.get('black_elo', '?')})
- Result: {metadata.get('result', '?')}
- Opening: {metadata.get('eco', '?')}

Moves:
{moves_str}

Provide TWO things:

1. **Move-by-move evaluation** - For EACH move, classify it into exactly ONE category:
   - **Brilliant** - Exceptional, hard-to-find move
   - **Excellent** - Very strong move
   - **Good** - Solid, reasonable move
   - **Inaccuracy** - Slightly suboptimal but not terrible
   - **Mistake** - Significant error
   - **Blunder** - Major error losing material or advantage
   - **Book** - Standard opening theory move

2. **Overall game analysis** with:
   - Opening assessment
   - Key turning points
   - Critical mistakes with better alternatives
   - Endgame notes
   - Lessons for both players

Format your response EXACTLY like this:

EVALUATIONS:
Move 1: Good - [brief reason]
Move 2: Book - [brief reason]
Move 3: Blunder - [brief reason]
... (one line per move, same number as moves above)

ANALYSIS:
[Your detailed game analysis here]"""

    try:
        headers = {"Content-Type": "application/json"}
        if LLM_API_KEY:
            headers["Authorization"] = f"Bearer {LLM_API_KEY}"
        
        response = requests.post(
            f"{LLM_API_URL}/v1/chat/completions",
            headers=headers,
            json={
                "model": "coder-model",
                "messages": [
                    {"role": "system", "content": "You are a chess grandmaster and expert coach."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 4000
            },
            timeout=600
        )
        
        if response.status_code == 200:
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
            return parse_llm_response(raw)
        else:
            raise Exception(f"LLM API returned status {response.status_code}: {response.text}")
    except requests.exceptions.ConnectionError:
        raise Exception(f"Cannot connect to LLM API at {LLM_API_URL}. Please ensure the server is running.")


def parse_llm_response(raw):
    """
    Parse LLM response into evaluations list and analysis text.
    Expected format:
    EVALUATIONS:
    Move 1: Good - reason
    ...
    ANALYSIS:
    text...
    """
    evaluations = []
    analysis_text = raw
    
    eval_section = False
    analysis_section = False
    
    for line in raw.split('\n'):
        stripped = line.strip()
        
        if stripped.startswith('EVALUATIONS:'):
            eval_section = True
            analysis_section = False
            continue
        elif stripped.startswith('ANALYSIS:'):
            eval_section = False
            analysis_section = True
            continue
        
        if eval_section:
            # Parse "Move N: Category - reason"
            match = re.match(r'Move\s+(\d+):\s*(\w+)\s*-?\s*(.*)', stripped)
            if match:
                move_num = int(match.group(1))
                category = match.group(2).strip()
                reason = match.group(3).strip()
                evaluations.append({
                    "move": move_num - 1,  # 0-indexed
                    "category": normalize_category(category),
                    "reason": reason
                })
    
    # If no EVALUATIONS section found, use entire response as analysis
    if not evaluations:
        analysis_text = raw
    
    return analysis_text, evaluations


def normalize_category(cat):
    """Normalize category names to a fixed set."""
    cat_lower = cat.lower()
    mapping = {
        'brilliant': 'brilliant',
        'excellent': 'excellent',
        'good': 'good',
        'book': 'book',
        'standard': 'book',
        'opening': 'book',
        'inaccuracy': 'inaccuracy',
        'inaccurate': 'inaccuracy',
        'mistake': 'mistake',
        'error': 'mistake',
        'blunder': 'blunder',
        'blundered': 'blunder',
    }
    return mapping.get(cat_lower, 'good')


def evaluate_with_stockfish(pgn, time_limit=0.5):
    """
    Use Stockfish to evaluate each position and classify moves.
    Returns list of evaluations with categories based on centipawn loss.
    """
    import logging
    stockfish_path = _find_stockfish()
    logging.info(f"Stockfish path: {stockfish_path}")
    
    if not stockfish_path:
        logging.warning("Stockfish not found")
        return None
    
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if not game:
            logging.warning("Failed to parse PGN")
            return None
        
        evaluations = []
        board = game.board()
        
        with chess.engine.SimpleEngine.popen_uci(stockfish_path) as engine:
            for move in game.mainline_moves():
                side_to_move = board.turn
                
                result = engine.analyse(board, chess.engine.Limit(time=time_limit))
                pv = result.get("pv", [])
                best_move = pv[0] if pv else move
                
                pov_score = result["score"].pov(side_to_move)
                if pov_score.is_mate():
                    best_cp = 10000 if pov_score.mate() > 0 else -10000
                else:
                    best_cp = pov_score.score()
                
                board.push(move)
                
                result2 = engine.analyse(board, chess.engine.Limit(time=time_limit))
                pov_score2 = result2["score"].pov(side_to_move)
                if pov_score2.is_mate():
                    actual_cp = 10000 if pov_score2.mate() > 0 else -10000
                else:
                    actual_cp = pov_score2.score()
                
                # Cap cp_loss at 500 for meaningful classification
                # (beyond that, position is already completely lost)
                cp_loss = min(500, max(0, best_cp - actual_cp))
                
                # Classify the move
                is_book = board.fullmove_number <= 10 and cp_loss < 20
                category, reason = _classify_move(cp_loss, board.turn, move, best_move, is_book)
                
                evaluations.append({
                    "category": category,
                    "reason": reason,
                    "cp_loss": cp_loss,
                    "score": _format_score(result2["score"])
                })
        
        logging.info(f"Generated {len(evaluations)} evaluations")
        return evaluations
    except Exception as e:
        import traceback
        logging.error(f"Stockfish error: {e}\n{traceback.format_exc()}")
        return None


def _find_stockfish():
    """Find stockfish binary on the system."""
    candidates = [
        "/usr/games/stockfish",      # Debian/Ubuntu package location
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish",
        "stockfish",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    import shutil
    return shutil.which("stockfish")


def _calc_centipawn_loss(board, before_score, after_score):
    """Calculate centipawn loss for the side that just moved."""
    # Convert scores to perspective of side that just moved
    turn = board.turn  # WHITE or BLACK - this is the side that will move NEXT
    # The side that just moved is the OPPOSITE
    moved_side = not turn
    
    def cp_for_pov(score, pov):
        return score.relative.score() if pov else -score.relative.score()
    
    before_cp = before_score.pov(moved_side).score()
    after_cp = after_score.pov(moved_side).score()
    
    # Clamp mate scores
    if before_cp > 10000: before_cp = 10000
    if before_cp < -10000: before_cp = -10000
    if after_cp > 10000: after_cp = 10000
    if after_cp < -10000: after_cp = -10000
    
    loss = max(0, before_cp - after_cp)
    return loss


def _classify_move(cp_loss, turn, move, best_move, is_book):
    """Classify a move based on centipawn loss."""
    if is_book:
        return "book", f"Standard opening theory move"
    
    if move == best_move and cp_loss < 5:
        return "excellent", "Best move according to engine"
    
    if cp_loss <= 15:
        return "excellent", f"Very strong move (loss: {cp_loss}cp)"
    elif cp_loss <= 50:
        return "good", f"Solid move (loss: {cp_loss}cp)"
    elif cp_loss <= 100:
        return "inaccuracy", f"Slightly suboptimal, better alternatives exist (loss: {cp_loss}cp)"
    elif cp_loss <= 250:
        return "mistake", f"Significant error, could be punished (loss: {cp_loss}cp)"
    elif cp_loss <= 500:
        return "blunder", f"Major error, likely losing material or position (loss: {cp_loss}cp)"
    else:
        return "blunder", f"Critical blunder, position severely damaged (loss: {cp_loss}cp)"


def _format_score(score):
    """Format score for display. score is a PovScore from engine.analyse()."""
    rel = score.relative
    if rel.is_mate():
        return f"M{abs(rel.mate())}"
    return f"{rel.score() / 100:+.2f}"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.json
    url = data.get("url", "").strip()
    
    if not url:
        return jsonify({"error": "Please provide a chess.com game URL"}), 400
    
    if "chess.com/game/" not in url:
        return jsonify({"error": "URL must be a chess.com game URL"}), 400
    
    game_type, game_id = parse_chesscom_url(url)
    
    if not game_id:
        return jsonify({"error": "Could not extract game ID from URL. Please check the format."}), 400
    
    try:
        pgn, metadata = fetch_chesscom_game(game_id, game_type)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch game: {str(e)}"}), 400
    
    try:
        # Use Stockfish for accurate move evaluations
        stockfish_evals = evaluate_with_stockfish(pgn, time_limit=0.1)
        
        # Use LLM for commentary on critical moments only
        analysis_text = ""
        try:
            analysis_text = _analyze_with_llm(pgn, metadata)
        except Exception as e:
            pass  # Continue without LLM analysis if it fails
        
        # Merge Stockfish evaluations with the moves
        evaluations = stockfish_evals or []
    except Exception as e:
        return jsonify({"error": f"Failed to analyze game: {str(e)}"}), 500
    
    # Parse the game to get board positions for each move
    board_positions = []
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if game:
            board = game.board()
            for move in game.mainline_moves():
                san = board.san(move)
                board.push(move)
                board_positions.append({
                    "uci": move.uci(),
                    "san": san,
                    "fen": board.fen()
                })
    except Exception:
        pass
    
    return jsonify({
        "pgn": pgn,
        "analysis": analysis_text,
        "evaluations": evaluations,
        "metadata": metadata,
        "moves": board_positions
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
