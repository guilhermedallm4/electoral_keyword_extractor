"""Armazenamento por data.

data/YYYY-MM-DD.json        saída do dia (keywords) — consumida pelo próximo estágio
data/state/YYYY-MM-DD.json  estado do dia: notícias eleitorais (título+resumo), candidatos do LLM,
                            última coleta — permite execução incremental e recálculo do dia
Um dia nunca lê o estado de outro: mudar a data = conjunto novo.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .collector import Article


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)  # escrita atômica


def output_path(data_dir: str, day: date) -> Path:
    return Path(data_dir) / f"{day.isoformat()}.json"


def state_path(data_dir: str, day: date) -> Path:
    return Path(data_dir) / "state" / f"{day.isoformat()}.json"


def load_state(data_dir: str, day: date, tz: str) -> dict:
    p = state_path(data_dir, day)
    if p.exists():
        st = json.loads(p.read_text())
        st["articles"] = [Article.from_dict(a) for a in st["articles"]]
        return st
    return {"date": day.isoformat(), "timezone": tz, "last_collection": None, "runs": [],
            "seen_today": [], "articles": []}


def save_state(data_dir: str, day: date, st: dict) -> None:
    _write_json(state_path(data_dir, day), {**st, "articles": [a.to_dict() for a in st["articles"]]})


def save_output(data_dir: str, day: date, out: dict) -> Path:
    p = output_path(data_dir, day)
    _write_json(p, out)
    return p


def load_output(data_dir: str, day: date) -> dict | None:
    p = output_path(data_dir, day)
    return json.loads(p.read_text()) if p.exists() else None
