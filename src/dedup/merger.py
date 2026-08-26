"""Fusão de contatos duplicados — a parte que não tem desfazer.

Ordem importa e não é negociável: **primeiro religar tudo, depois apagar**.
Se o robô morrer no meio, o pior cenário é um contato órfão duplicado (que a
próxima execução reencontra); na ordem inversa, o pior cenário é um negócio
sem contato nenhum, e nada no CRM lembra a quem ele pertencia.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bitrix24_client import Bitrix24

from .matching import Contato, Par

log = logging.getLogger("dedup")

#: Entidades que apontam para um contato e precisam ser religadas antes do apagão.
VINCULOS = (
    ("crm.deal.list", "crm.deal.update", "CONTACT_ID"),
    ("crm.lead.list", "crm.lead.update", "CONTACT_ID"),
    ("crm.quote.list", "crm.quote.update", "CONTACT_ID"),
)


@dataclass
class RelatorioFusao:
    """Resultado da execução, para o CSV e para o resumo."""

    fundidos: int = 0
    vinculos_movidos: int = 0
    atividades_movidas: int = 0
    ignorados: int = 0
    falhas: list[tuple[int, str]] = field(default_factory=list)

    def resumo(self) -> str:
        return (
            f"{self.fundidos} fusoes | {self.vinculos_movidos} vinculos movidos | "
            f"{self.atividades_movidas} atividades movidas | "
            f"{self.ignorados} ignorados | {len(self.falhas)} falhas"
        )


class Merger:
    """Executa a fusão de um par já aprovado."""

    def __init__(self, bx: Bitrix24, *, dry_run: bool = True) -> None:
        self.bx = bx
        self.dry_run = dry_run

    def fundir(self, par: Par, relatorio: RelatorioFusao) -> None:
        """Completa o sobrevivente, religa os vínculos e só então apaga.

        Args:
            par: dupla já aprovada, com ``manter`` e ``remover`` definidos.
            relatorio: acumulador atualizado no lugar.
        """
        manter, remover = par.manter, par.remover
        if manter.id == remover.id:
            relatorio.ignorados += 1
            return

        try:
            self._completar_campos(manter, remover)
            relatorio.vinculos_movidos += self._religar_vinculos(manter.id, remover.id)
            relatorio.atividades_movidas += self._mover_atividades(manter.id, remover.id)
            self._registrar_na_timeline(manter.id, remover, par)
            self._apagar(remover.id)
            relatorio.fundidos += 1
        except Exception as exc:  # noqa: BLE001 - uma fusao ruim nao para o lote
            log.exception("falha ao fundir %d em %d", remover.id, manter.id)
            relatorio.falhas.append((remover.id, str(exc)))

    def _completar_campos(self, manter: Contato, remover: Contato) -> None:
        """Leva para o sobrevivente o que só o removido tinha.

        Nunca sobrescreve valor existente: o sobrevivente foi escolhido por
        ser o mais completo, então o que ele já tem é a versão preferida.
        Só e-mails e telefones ausentes são acrescentados.
        """
        novos_emails = remover.emails - manter.emails
        novos_telefones = remover.telefones - manter.telefones
        if not novos_emails and not novos_telefones:
            return

        campos: dict[str, Any] = {}
        if novos_emails:
            campos["EMAIL"] = [
                *({"VALUE": e, "VALUE_TYPE": "WORK"} for e in manter.emails),
                *({"VALUE": e, "VALUE_TYPE": "OTHER"} for e in novos_emails),
            ]
        if novos_telefones:
            campos["PHONE"] = [
                *({"VALUE": t, "VALUE_TYPE": "WORK"} for t in manter.telefones),
                *({"VALUE": t, "VALUE_TYPE": "OTHER"} for t in novos_telefones),
            ]

        if self.dry_run:
            log.info("[simulacao] completaria contato %d com %s", manter.id, list(campos))
            return
        self.bx.call("crm.contact.update", {"id": manter.id, "fields": campos})

    def _religar_vinculos(self, manter_id: int, remover_id: int) -> int:
        """Aponta negócios, leads e propostas para o sobrevivente."""
        movidos = 0
        for metodo_lista, metodo_update, campo in VINCULOS:
            registros = list(
                self.bx.fetch_all(
                    metodo_lista, {"filter": {campo: remover_id}, "select": ["ID"]}
                )
            )
            if not registros:
                continue
            if self.dry_run:
                log.info(
                    "[simulacao] moveria %d registros de %s", len(registros), metodo_lista
                )
                movidos += len(registros)
                continue

            for registro, _, erro in self.bx.batch_iter(
                registros, metodo_update, lambda r: {"id": r["ID"], "fields": {campo: manter_id}}
            ):
                if erro:
                    log.error("nao movi %s %s: %s", metodo_lista, registro["ID"], erro)
                else:
                    movidos += 1
        return movidos

    def _mover_atividades(self, manter_id: int, remover_id: int) -> int:
        """Transfere ligações, e-mails e tarefas ligadas ao contato removido.

        Sem isso o histórico de conversa desaparece junto com o duplicado — e
        o vendedor liga para o cliente sem saber que já falaram ontem.
        """
        atividades = list(
            self.bx.fetch_all(
                "crm.activity.list",
                {"filter": {"OWNER_TYPE_ID": 3, "OWNER_ID": remover_id}, "select": ["ID"]},
            )
        )
        if not atividades:
            return 0
        if self.dry_run:
            log.info("[simulacao] moveria %d atividades", len(atividades))
            return len(atividades)

        movidas = 0
        for _, _, erro in self.bx.batch_iter(
            atividades,
            "crm.activity.update",
            lambda a: {"id": a["ID"], "fields": {"OWNER_ID": manter_id, "OWNER_TYPE_ID": 3}},
        ):
            movidas += 0 if erro else 1
        return movidas

    def _registrar_na_timeline(self, manter_id: int, remover: Contato, par: Par) -> None:
        """Deixa rastro da fusão no contato que ficou.

        Fusão não tem desfazer. Quem abrir o contato daqui a seis meses e
        estranhar um telefone precisa conseguir descobrir de onde ele veio.
        """
        texto = (
            f"Fusao automatica: contato #{remover.id} ({remover.nome or 'sem nome'}) "
            f"foi incorporado a este registro. "
            f"Confianca {par.pontuacao:.0%} — {'; '.join(par.sinais)}."
        )
        if self.dry_run:
            log.info("[simulacao] timeline de %d: %s", manter_id, texto)
            return
        self.bx.call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": manter_id, "ENTITY_TYPE": "contact", "COMMENT": texto}},
        )

    def _apagar(self, contato_id: int) -> None:
        if self.dry_run:
            log.info("[simulacao] apagaria contato %d", contato_id)
            return
        self.bx.call("crm.contact.delete", {"id": contato_id})


def exportar_para_revisao(pares: list[Par], destino: Path) -> None:
    """Grava o CSV que uma pessoa aprova antes de qualquer fusão acontecer.

    A coluna ``aprovar`` vem em branco de propósito: quem revisa marca ``s``
    nas linhas que deseja fundir. Default vazio significa que esquecer de
    revisar não funde nada.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8-sig", newline="") as fh:
        escritor = csv.writer(fh, delimiter=";")
        escritor.writerow(
            ["aprovar", "manter_id", "manter_nome", "remover_id", "remover_nome",
             "confianca", "por_que"]
        )
        for par in pares:
            escritor.writerow(["", *par.linha_csv()])


def ler_aprovados(origem: Path, pares: list[Par]) -> list[Par]:
    """Filtra os pares que o revisor marcou com ``s`` na coluna ``aprovar``."""
    aprovados: set[tuple[int, int]] = set()
    with origem.open("r", encoding="utf-8-sig", newline="") as fh:
        for linha in csv.DictReader(fh, delimiter=";"):
            if (linha.get("aprovar") or "").strip().lower() in {"s", "sim", "x", "1"}:
                aprovados.add((int(linha["manter_id"]), int(linha["remover_id"])))
    return [p for p in pares if (p.manter.id, p.remover.id) in aprovados]
