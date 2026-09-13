"""轻量项目记忆：W3 先提供可持久化人设卡。"""

from .persona import Persona, list_personas, load_persona, parse_persona, save_persona

__all__ = ["Persona", "list_personas", "load_persona", "parse_persona", "save_persona"]
