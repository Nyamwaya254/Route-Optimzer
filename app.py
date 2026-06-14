"""
Field Sales Route Optimizer — Web Application
==============================================
Agents select their name from a searchable list and receive a live-generated
route map (typically 30–60 seconds using cached road graphs).

Run:
    python app.py
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request

from route_optimizer import RouteResult, RouteService

DEFAULT_PORT = 8080
PORT = int(os.environ["PORT"]) if "PORT" in os.environ else DEFAULT_PORT

app = Flask(__name__)
route_service = RouteService()

_jobs_lock = threading.Lock()
_jobs: dict[str, "RouteJob"] = {}


@dataclass
class RouteJob:
    job_id: str
    agent_name: str
    status: str = "pending"  # pending | running | complete | failed
    result: Optional[RouteResult] = None
    error: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _run_route_job(job_id: str, agent_name: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.status = "running"

    try:
        result = route_service.generate_route(agent_name, save_map=False)
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            if result.success:
                job.status = "complete"
                job.result = result
            else:
                job.status = "failed"
                job.error = result.error or "Route generation failed."
    except Exception as exc:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)


@app.route("/")
def index():
    return render_template("index.html", app_url=f"http://localhost:{PORT}")


@app.route("/api/agents")
def list_agents():
    try:
        agents = route_service.list_agents()
        return jsonify({"agents": agents})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/routes", methods=["POST"])
def start_route():
    data = request.get_json(silent=True) or {}
    agent_name = (data.get("agent_name") or "").strip()
    if not agent_name:
        return jsonify({"error": "agent_name is required."}), 400

    job_id = str(uuid.uuid4())
    job = RouteJob(job_id=job_id, agent_name=agent_name)
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(
        target=_run_route_job, args=(job_id, agent_name), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "pending"}), 202


@app.route("/api/routes/<job_id>")
def route_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found."}), 404

    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "agent_name": job.agent_name,
        "status": job.status,
    }

    if job.status == "complete" and job.result:
        payload["summary"] = job.result.summary
        payload["sub_cluster"] = job.result.sub_cluster
        payload["map_html"] = job.result.map_html
    elif job.status == "failed":
        payload["error"] = job.error or "Route generation failed."

    return jsonify(payload)


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
            return True
        except OSError:
            return False


def _resolve_port() -> int:
    if "PORT" in os.environ:
        return int(os.environ["PORT"])

    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        if _port_is_free(port):
            return port

    raise RuntimeError(
        f"No free port found between {DEFAULT_PORT} and {DEFAULT_PORT + 19}. "
        "Stop other servers or set PORT manually, e.g. PORT=9000 python app.py"
    )


if __name__ == "__main__":
    PORT = _resolve_port()
    route_service.initialize()
    if PORT != DEFAULT_PORT and "PORT" not in os.environ:
        print(f"Port {DEFAULT_PORT} is in use — starting on {PORT} instead.")
    print(f"\n  Open http://localhost:{PORT} in your browser\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
