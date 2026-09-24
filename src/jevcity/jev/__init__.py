"""Isolated Jev adapter: real HTTP, weighted-random MOCK, and REPLAY from logs. Owner: T3."""

from jevcity.types import CallSink, JevBackend, JevConfig


def make_backend(cfg: JevConfig, sink: CallSink | None = None) -> JevBackend:
    """Factory by cfg.mode. Real mode reads the key from os.environ[cfg.api_key_env]."""
    raise NotImplementedError
