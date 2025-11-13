import json
from datetime import datetime
from pathlib import Path


class EmailPriorityClassifierLogger:
    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        log_dir.mkdir(exist_ok=True)

    @staticmethod
    def _parse_log_json(data: str) -> str:
        try:
            d = json.loads(data)
            for k, v in d.items():
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
            return json.dumps(d, indent=4)
        except Exception:
            return data

    def log_request_operated(self, ai: str, model: str, request_id: str, request: str, response: str, request_at: datetime, response_at: datetime):
        log_file = self._log_dir / f"{request_id}.log"
        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"AI: {ai}\n")
            f.write(f"Model: {model}\n")
            f.write(f"Request ID: {request_id}\n")
            f.write(f"Request At: {request_at.isoformat()}\n")
            f.write(f"Response At: {response_at.isoformat()}\n")
            f.write(f"Duration: {str(response_at - request_at)}\n")
            f.write(f"\n")

            f.write("----- Request -----\n")
            f.write(self._parse_log_json(request) + "\n")
            f.write(f"\n")

            f.write("----- Response -----\n")
            f.write(self._parse_log_json(response) + "\n")
        return request_id, log_file

    def log_request_error(self, ai: str, model: str, request_id: str, request: str, error_message: str, exception, request_at: datetime,
                          response_at: datetime):
        log_file = self._log_dir / f"{request_id}_error.log"
        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"AI: {ai}\n")
            f.write(f"Model: {model}\n")
            f.write(f"Request ID: {request_id}\n")
            f.write(f"Request At: {request_at.isoformat()}\n")
            f.write(f"\n")

            f.write("----- Request -----\n")
            f.write(self._parse_log_json(request) + "\n")
            f.write(f"\n")

            f.write("----- Error -----\n")
            f.write(self._parse_log_json(error_message) + "\n")

            if exception:
                f.write(f"\n")
                f.write("----- Exception -----\n")
                f.write(str(exception) + "\n")
        return request_id, log_file
