"""Cliente mínimo para um servidor OpenAI-compatível (llama-server com o Bonsai-27B).

Só o necessário para a extração: uma chamada sem histórico com saída restrita a um JSON schema
(gramática do llama-server). O servidor é o de ~/bonsai_agent (start_server.sh).
"""
from __future__ import annotations

import json
import time

import requests


class LLMClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080/v1", timeout: float = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def wait_ready(self, timeout: float = 300):
        """Espera o servidor terminar de carregar o modelo (/health -> 200)."""
        url = self.base_url.removesuffix("/v1") + "/health"
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if requests.get(url, timeout=5).status_code == 200:
                    return
            except requests.ConnectionError:
                pass
            time.sleep(2)
        raise RuntimeError(f"servidor LLM não respondeu em {timeout}s ({url})")

    def complete_json(self, prompt: str, schema: dict, system: str = "You are a helpful assistant",
                      temperature: float = 0.3, max_tokens: int = 4096, thinking: bool = False) -> dict:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.95, "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": thinking},
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
        }
        r = requests.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
