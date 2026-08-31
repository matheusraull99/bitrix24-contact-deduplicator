"""Linha de comando do deduplicador, em dois passos deliberados.

`analisar` gera um CSV de revisão. `fundir` só executa o que uma pessoa
marcou nesse CSV. Não existe caminho de um comando só que apague contato —
fusão não tem desfazer, e a economia de um passo não paga o risco.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bitrix24_client import from_env
from bitrix24_client.errors import BitrixError

from .matching import Contato, encontrar_pares
from .merger import Merger, RelatorioFusao, exportar_para_revisao, ler_aprovados

log = logging.getLogger("dedup")

CAMPOS = [
    "ID", "NAME", "LAST_NAME", "EMAIL", "PHONE",
    "COMPANY_TITLE", "DATE_CREATE", "UF_CRM_DOCUMENTO",
]


def carregar_contatos(bx) -> list[Contato]:
    """Baixa a base inteira já achatada para o formato de pareamento."""
    brutos = list(bx.fetch_all("crm.contact.list", {"select": CAMPOS}))
    log.info("%d contatos carregados", len(brutos))
    return [Contato.do_crm(b) for b in brutos]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="dedup-contatos",
        description="Encontra e funde contatos duplicados no Bitrix24.",
    )
    sub = p.add_subparsers(dest="comando", required=True)

    an = sub.add_parser("analisar", help="gera o CSV de revisao")
    an.add_argument("--saida", type=Path, default=Path("saida/duplicados.csv"))
    an.add_argument("--limiar", type=float, default=0.75)

    fu = sub.add_parser("fundir", help="funde os pares aprovados no CSV")
    fu.add_argument("--csv", type=Path, required=True)
    fu.add_argument("--limiar", type=float, default=0.75)
    fu.add_argument("--executar", action="store_true", help="grava de verdade")

    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        bx = from_env()
        contatos = carregar_contatos(bx)
        pares = encontrar_pares(contatos, limiar=args.limiar)
    except BitrixError as exc:
        print(f"erro no portal: {exc}", file=sys.stderr)
        return 2

    if args.comando == "analisar":
        exportar_para_revisao(pares, args.saida)
        print(f"\n{len(pares)} pares acima de {args.limiar:.0%} -> {args.saida}")
        for par in pares[:10]:
            print(
                f"  {par.pontuacao:.0%}  manter #{par.manter.id} {par.manter.nome[:28]:<28}"
                f" | remover #{par.remover.id} {par.remover.nome[:28]:<28}"
                f" | {'; '.join(par.sinais)}"
            )
        print("\nRevise o CSV, marque 's' na coluna 'aprovar' e rode:")
        print(f"  dedup-contatos fundir --csv {args.saida} --executar")
        return 0

    if not args.csv.exists():
        print(f"CSV nao encontrado: {args.csv}", file=sys.stderr)
        return 2

    aprovados = ler_aprovados(args.csv, pares)
    if not aprovados:
        print("nenhum par marcado com 's' na coluna 'aprovar'")
        return 0

    merger = Merger(bx, dry_run=not args.executar)
    relatorio = RelatorioFusao()
    for par in aprovados:
        merger.fundir(par, relatorio)

    modo = "EXECUTADO" if args.executar else "SIMULACAO (use --executar)"
    print(f"\n{modo}\n{relatorio.resumo()}")
    for contato_id, erro in relatorio.falhas[:10]:
        print(f"  contato {contato_id}: {erro}")
    return 1 if relatorio.falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
