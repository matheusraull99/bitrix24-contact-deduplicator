"""Deduplicador de contatos do Bitrix24 com blocking e revisao humana."""

from .matching import (
    Contato,
    Par,
    encontrar_pares,
    escolher_sobrevivente,
    fonetico_br,
    pontuar,
    tokens_significativos,
)
from .merger import Merger, RelatorioFusao, exportar_para_revisao, ler_aprovados

__version__ = "1.0.0"

__all__ = [
    "Contato",
    "Merger",
    "Par",
    "RelatorioFusao",
    "encontrar_pares",
    "escolher_sobrevivente",
    "exportar_para_revisao",
    "fonetico_br",
    "ler_aprovados",
    "pontuar",
    "tokens_significativos",
]
