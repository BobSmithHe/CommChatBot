"""Run JSONL Agent eval cases against the public streaming API.

Each line accepts: {"prompt":"...", "mode":"chatbot", "contains":["expected"]}.
"""
from __future__ import annotations

import argparse
import json

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    cases = [json.loads(line) for line in open(args.cases, encoding="utf-8") if line.strip()]
    passed = 0
    with httpx.Client(base_url=args.url, headers=headers, timeout=180, trust_env=False) as client:
        for index, case in enumerate(cases, start=1):
            answer = ""
            with client.stream("POST", "/api/chat/stream", json={
                "message": case["prompt"], "mode": case.get("mode", "chatbot"),
                "use_rag": case.get("use_rag", False), "use_web": case.get("use_web", False),
            }) as response:
                response.raise_for_status()
                event = ""
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:") and event == "answer":
                        answer += str(json.loads(line[5:].strip()))
            expected = case.get("contains", [])
            ok = all(str(item).casefold() in answer.casefold() for item in expected)
            passed += int(ok)
            print(json.dumps({"case": index, "passed": ok, "answer_chars": len(answer)}))
    print(json.dumps({"passed": passed, "total": len(cases), "score": passed / max(1, len(cases))}))


if __name__ == "__main__":
    main()
