#!/usr/bin/env python3
"""Regenera os 14 workflows n8n com o header X-OMNAI-Token.

Uso:
    OMNAI_API_TOKEN=xxx python3 scripts/regenerate_workflows.py \\
        --schedules /opt/omnai-stack/schedules/schedules.json \\
        --out /opt/omnai-stack/schedules/n8n_workflows

Lê o schedules.json e regenera todos os workflows n8n com:
    - url: https://agents.omnai.pt/tasks/run/<taskId>
    - POST JSON
    - Header X-OMNAI-Token
    - Campo id = <taskId> (para o import:workflow do n8n)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys


def build_workflow(task: dict, token: str) -> dict:
    tid = task["taskId"]
    cron = task["cronExpression"]

    return {
        "id": tid,
        "name": f"OMNAI - {tid}",
        "active": False,
        "nodes": [
            {
                "parameters": {
                    "triggerTimes": {
                        "item": [
                            {"mode": "custom", "cronExpression": cron}
                        ]
                    }
                },
                "name": "Cron",
                "type": "n8n-nodes-base.cron",
                "typeVersion": 1,
                "position": [260, 300],
            },
            {
                "parameters": {
                    "url": f"https://agents.omnai.pt/tasks/run/{tid}",
                    "method": "POST",
                    "options": {"timeout": 120000},
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [
                            {"name": "X-OMNAI-Token", "value": token}
                        ]
                    },
                    "sendBody": True,
                    "bodyContentType": "json",
                    "jsonBody": '={\n  "source": "n8n",\n  "payload": {}\n}',
                },
                "name": "HTTP Request",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4,
                "position": [520, 300],
            },
            {
                "parameters": {
                    "values": {
                        "string": [
                            {"name": "status", "value": "={{$json[\"status\"]}}"},
                            {"name": "taskId", "value": tid},
                        ]
                    }
                },
                "name": "Log",
                "type": "n8n-nodes-base.set",
                "typeVersion": 2,
                "position": [780, 300],
            },
        ],
        "connections": {
            "Cron": {
                "main": [[{"node": "HTTP Request", "type": "main", "index": 0}]]
            },
            "HTTP Request": {
                "main": [[{"node": "Log", "type": "main", "index": 0}]]
            },
        },
        "settings": {"timezone": "Europe/Lisbon"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedules", required=True, help="Path para schedules.json")
    ap.add_argument("--out", required=True, help="Pasta de saida para os workflows")
    ap.add_argument("--token", default=os.getenv("OMNAI_API_TOKEN", ""))
    args = ap.parse_args()

    if not args.token:
        print("ERRO: token nao fornecido. Define OMNAI_API_TOKEN ou usa --token.",
              file=sys.stderr)
        return 2

    schedules_path = pathlib.Path(args.schedules)
    out_path = pathlib.Path(args.out)
    out_path.mkdir(parents=True, exist_ok=True)

    tasks = json.loads(schedules_path.read_text(encoding="utf-8"))
    count = 0
    for t in tasks:
        wf = build_workflow(t, args.token)
        fpath = out_path / f"{t['taskId']}.json"
        fpath.write_text(
            json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  -> {fpath}")
        count += 1

    print(f"\nGerados {count} workflows em {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
