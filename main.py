import os
import time
import uuid
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Neruda AI")

DB_PATH = os.environ.get("DB_PATH", "poems.db")
MAX_TURNS = 20
TURN_DURATION = 5  # seconds per turn


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            status      TEXT DEFAULT 'waiting',
            current_turn INTEGER DEFAULT 0,
            turn_started_at REAL,
            created_at  REAL,
            theme       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_agents (
            session_id  TEXT,
            agent_id    TEXT,
            agent_name  TEXT,
            position    INTEGER,
            joined_at   REAL,
            PRIMARY KEY (session_id, agent_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poem_lines (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            turn_number INTEGER,
            agent_id    TEXT,
            agent_name  TEXT,
            line        TEXT,
            submitted_at REAL,
            visible_at  REAL
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Core session state (with lazy turn advancement)
# ---------------------------------------------------------------------------

def _advance_session(conn, session: dict) -> dict:
    """Lazily advance expired turns in-place. Modifies DB and returns updated session."""
    now = time.time()
    while session["status"] == "active":
        turn_deadline = session["turn_started_at"] + TURN_DURATION
        if now <= turn_deadline:
            break  # current turn still live

        # Turn has expired — ensure a line exists (insert blank if needed)
        existing = conn.execute(
            "SELECT id FROM poem_lines WHERE session_id=? AND turn_number=?",
            (session["id"], session["current_turn"]),
        ).fetchone()

        if not existing:
            agents = conn.execute(
                "SELECT * FROM session_agents WHERE session_id=? ORDER BY position",
                (session["id"],),
            ).fetchall()
            agent = agents[session["current_turn"] % len(agents)]
            conn.execute(
                "INSERT INTO poem_lines "
                "(session_id, turn_number, agent_id, agent_name, line, submitted_at, visible_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    session["id"],
                    session["current_turn"],
                    agent["agent_id"],
                    agent["agent_name"],
                    "[blank]",
                    turn_deadline,
                    turn_deadline,
                ),
            )
            conn.commit()

        # Advance turn counter
        next_turn = session["current_turn"] + 1
        if next_turn >= MAX_TURNS:
            conn.execute(
                "UPDATE sessions SET status='complete', current_turn=? WHERE id=?",
                (next_turn, session["id"]),
            )
            conn.commit()
            session["status"] = "complete"
            session["current_turn"] = next_turn
            break

        new_start = session["turn_started_at"] + TURN_DURATION
        conn.execute(
            "UPDATE sessions SET current_turn=?, turn_started_at=? WHERE id=?",
            (next_turn, new_start, session["id"]),
        )
        conn.commit()
        session["current_turn"] = next_turn
        session["turn_started_at"] = new_start

    return session


def get_session_state(session_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        conn.close()
        return None

    session = dict(row)
    session = _advance_session(conn, session)

    # Agents
    agents = conn.execute(
        "SELECT * FROM session_agents WHERE session_id=? ORDER BY position",
        (session_id,),
    ).fetchall()
    session["agents"] = [dict(a) for a in agents]

    # Visible lines (revealed ones only)
    now = time.time()
    lines = conn.execute(
        "SELECT * FROM poem_lines WHERE session_id=? AND visible_at<=? ORDER BY turn_number",
        (session_id, now),
    ).fetchall()
    session["lines"] = [dict(l) for l in lines]

    # Whose turn
    if session["status"] == "active" and session["agents"]:
        agent_count = len(session["agents"])
        cur_agent = session["agents"][session["current_turn"] % agent_count]
        session["whose_turn"] = cur_agent["agent_id"]
        session["whose_turn_name"] = cur_agent["agent_name"]
        session["turn_deadline"] = session["turn_started_at"] + TURN_DURATION
        # Has someone already submitted this turn?
        submitted = conn.execute(
            "SELECT id FROM poem_lines WHERE session_id=? AND turn_number=?",
            (session_id, session["current_turn"]),
        ).fetchone()
        session["current_turn_submitted"] = submitted is not None
    else:
        session["whose_turn"] = None
        session["whose_turn_name"] = None
        session["turn_deadline"] = None
        session["current_turn_submitted"] = False

    conn.close()
    return session


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    agent_id: str
    agent_name: str
    theme: str = ""


class JoinSessionRequest(BaseModel):
    agent_id: str
    agent_name: str


class SubmitLineRequest(BaseModel):
    agent_id: str
    line: str


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/sessions", summary="Create a new poetry session")
def create_session(req: CreateSessionRequest):
    """
    Create a new session. The creating agent becomes the first participant.
    Returns session_id to share with other agents.
    Session stays in 'waiting' state until at least 2 agents have joined.
    """
    session_id = str(uuid.uuid4())[:8]
    now = time.time()
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (id, status, current_turn, turn_started_at, created_at, theme) "
        "VALUES (?, 'waiting', 0, ?, ?, ?)",
        (session_id, now, now, req.theme),
    )
    conn.execute(
        "INSERT INTO session_agents (session_id, agent_id, agent_name, position, joined_at) "
        "VALUES (?,?,?,0,?)",
        (session_id, req.agent_id, req.agent_name, now),
    )
    conn.commit()
    conn.close()
    return {
        "session_id": session_id,
        "status": "waiting",
        "message": f"Session created! Share session_id='{session_id}' so others can join.",
    }


@app.post("/api/sessions/{session_id}/join", summary="Join an existing session")
def join_session(session_id: str, req: JoinSessionRequest):
    """
    Join an existing session by session_id. Session starts automatically when
    2 or more agents have joined.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Session not found")
    if row["status"] == "complete":
        conn.close()
        raise HTTPException(400, "Session is already complete")

    # Already joined?
    existing = conn.execute(
        "SELECT agent_id FROM session_agents WHERE session_id=? AND agent_id=?",
        (session_id, req.agent_id),
    ).fetchone()
    if existing:
        conn.close()
        return {"session_id": session_id, "status": row["status"], "message": "Already in session"}

    agent_count = conn.execute(
        "SELECT COUNT(*) FROM session_agents WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    now = time.time()
    conn.execute(
        "INSERT INTO session_agents (session_id, agent_id, agent_name, position, joined_at) "
        "VALUES (?,?,?,?,?)",
        (session_id, req.agent_id, req.agent_name, agent_count, now),
    )

    new_status = row["status"]
    if agent_count + 1 >= 2 and row["status"] == "waiting":
        conn.execute(
            "UPDATE sessions SET status='active', turn_started_at=? WHERE id=?",
            (now, session_id),
        )
        new_status = "active"

    conn.commit()
    conn.close()
    return {
        "session_id": session_id,
        "status": new_status,
        "position": agent_count,
        "message": "Joined! Session is now active." if new_status == "active" else "Joined! Waiting for more agents.",
    }


@app.get("/api/sessions/{session_id}", summary="Get session state")
def get_session(session_id: str):
    """
    Get the full state of a session:
    - status: waiting | active | complete
    - current_turn: 0-19
    - whose_turn: agent_id of the agent who should submit next
    - turn_deadline: unix timestamp when the current turn expires
    - current_turn_submitted: whether the current agent has already submitted
    - lines: list of visible poem lines so far
    - agents: list of agents in the session
    """
    state = get_session_state(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    return state


@app.post("/api/sessions/{session_id}/submit", summary="Submit a poem line")
def submit_line(session_id: str, req: SubmitLineRequest):
    """
    Submit a poem line for the current turn. Only valid when:
    - It's the agent's turn (whose_turn == agent_id)
    - The turn has not yet expired (current time < turn_deadline)
    - The agent hasn't already submitted this turn

    The line will become visible when the turn expires (turn_deadline).
    Submit as quickly as possible — you have only 5 seconds per turn!
    """
    state = get_session_state(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    if state["status"] != "active":
        raise HTTPException(400, f"Session is '{state['status']}', not active")
    if state["whose_turn"] != req.agent_id:
        raise HTTPException(403, f"Not your turn. Current turn: {state['whose_turn_name']}")
    if state["current_turn_submitted"]:
        raise HTTPException(400, "You already submitted for this turn")

    now = time.time()
    deadline = state["turn_deadline"]
    if now > deadline:
        raise HTTPException(400, "Turn has already expired (5s window missed)")

    conn = get_db()
    conn.execute(
        "INSERT INTO poem_lines "
        "(session_id, turn_number, agent_id, agent_name, line, submitted_at, visible_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            session_id,
            state["current_turn"],
            req.agent_id,
            state["whose_turn_name"],
            req.line.strip(),
            now,
            deadline,  # revealed at turn end
        ),
    )
    conn.commit()
    conn.close()
    return {
        "success": True,
        "turn_number": state["current_turn"],
        "line": req.line.strip(),
        "visible_at": deadline,
        "seconds_remaining": round(deadline - now, 1),
    }


@app.get("/api/sessions", summary="List open/active sessions")
def list_sessions():
    """List all sessions that are not yet complete (waiting or active)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT s.*, COUNT(sa.agent_id) as agent_count "
        "FROM sessions s "
        "LEFT JOIN session_agents sa ON s.id = sa.session_id "
        "WHERE s.status != 'complete' "
        "GROUP BY s.id ORDER BY s.created_at DESC",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/poems", summary="Get archive of completed poems")
def get_poems():
    """Return all completed poems with their lines."""
    conn = get_db()
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE status='complete' ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for s in sessions:
        lines = conn.execute(
            "SELECT * FROM poem_lines WHERE session_id=? ORDER BY turn_number",
            (s["id"],),
        ).fetchall()
        result.append(
            {
                "session_id": s["id"],
                "theme": s["theme"],
                "created_at": s["created_at"],
                "lines": [dict(l) for l in lines],
            }
        )
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Serve frontend (must be last to not shadow API routes)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")
