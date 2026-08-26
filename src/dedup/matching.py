"""Pareamento de contatos duplicados sem comparar todo mundo com todo mundo.

Um portal com 40 mil contatos daria 800 milhões de comparações na força
bruta. A saída é *blocking*: agrupar candidatos por chaves baratas — final do
telefone, e-mail, código fonético do nome — e só pontuar quem cai no mesmo
bloco. O custo vira quase linear e a recuperação continua alta, porque
duplicata de verdade quase sempre compartilha pelo menos uma dessas chaves.

O código fonético é adaptado ao português: `Gonçalves`/`Goncalves`,
`Souza`/`Sousa` e `Xavier`/`Chavier` precisam colidir, e o Soundex inglês
não faz isso.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

#: Palavras que não distinguem uma empresa da outra e atrapalham a pontuação.
_RUIDO = frozenset(
    {
        "ltda", "me", "epp", "sa", "eireli", "mei", "cia", "comercio", "servicos",
        "industria", "e", "de", "da", "do", "das", "dos", "the",
    }
)


def sem_acento(texto: str) -> str:
    """Remove diacríticos preservando as letras."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def fonetico_br(nome: str) -> str:
    """Código fonético aproximado para nomes em português.

    As substituições cobrem as confusões que produzem duplicata de verdade
    em base brasileira, na ordem em que precisam ser aplicadas:

    * dígrafos primeiro (``ch``→``x``, ``lh``→``l``, ``nh``→``n``);
    * sibilantes depois (``ç``, ``ss``, ``z`` final, ``sc`` → ``s``);
    * vogais caem no fim, porque é onde mora a maior parte da variação.

    >>> fonetico_br("Gonçalves") == fonetico_br("Goncalvez")
    True
    >>> fonetico_br("Souza") == fonetico_br("Sousa")
    True
    """
    t = sem_acento(nome.lower())
    t = re.sub(r"[^a-z\s]", "", t)

    substituicoes = [
        (r"ch", "x"), (r"lh", "l"), (r"nh", "n"), (r"rr", "r"), (r"ss", "s"),
        (r"sc", "s"), (r"sç", "s"), (r"ç", "s"), (r"^h", ""), (r"ph", "f"),
        (r"gu([ei])", r"g\1"), (r"qu([ei])", r"k\1"), (r"q", "k"),
        (r"c([eiy])", r"s\1"), (r"c", "k"), (r"z$", "s"), (r"z", "s"),
        (r"y", "i"), (r"w", "v"),
    ]
    for padrao, troca in substituicoes:
        t = re.sub(padrao, troca, t)

    # Vogais só sobrevivem na primeira posição de cada palavra.
    palavras = [p[0] + re.sub(r"[aeiou]", "", p[1:]) if p else "" for p in t.split()]
    # Consoante repetida colada vira uma só: "Anna" e "Ana" devem colidir.
    return " ".join(re.sub(r"(.)\1+", r"\1", p) for p in palavras if p)


def tokens_significativos(texto: str) -> frozenset[str]:
    """Palavras do nome sem acento, sem ruído societário e sem palavra de 1 letra."""
    limpo = re.sub(r"[^a-z0-9\s]", " ", sem_acento(texto.lower()))
    return frozenset(t for t in limpo.split() if len(t) > 1 and t not in _RUIDO)


@dataclass(frozen=True)
class Contato:
    """Contato achatado, do jeito que o pareamento precisa dele."""

    id: int
    nome: str = ""
    emails: frozenset[str] = field(default_factory=frozenset)
    telefones: frozenset[str] = field(default_factory=frozenset)
    empresa: str = ""
    documento: str = ""
    criado_em: str = ""
    campos_preenchidos: int = 0

    @classmethod
    def do_crm(cls, bruto: dict[str, Any]) -> Contato:
        """Achata o payload do ``crm.contact.list``.

        ``EMAIL`` e ``PHONE`` chegam como lista de dicionários, e um contato
        pode ter três telefones. Reduzir a conjuntos deixa a interseção
        trivial mais adiante.
        """
        nome = " ".join(
            filter(None, [bruto.get("NAME", ""), bruto.get("LAST_NAME", "")])
        ).strip()
        emails = frozenset(
            e["VALUE"].strip().lower() for e in bruto.get("EMAIL") or [] if e.get("VALUE")
        )
        telefones = frozenset(
            re.sub(r"\D", "", t["VALUE"])[-8:]  # os 8 finais ignoram DDI e DDD
            for t in bruto.get("PHONE") or []
            if t.get("VALUE") and len(re.sub(r"\D", "", t["VALUE"])) >= 8
        )
        return cls(
            id=int(bruto["ID"]),
            nome=nome,
            emails=emails,
            telefones=telefones,
            empresa=bruto.get("COMPANY_TITLE", "") or "",
            documento=re.sub(r"\D", "", bruto.get("UF_CRM_DOCUMENTO", "") or ""),
            criado_em=bruto.get("DATE_CREATE", "") or "",
            campos_preenchidos=sum(1 for v in bruto.values() if v not in (None, "", [])),
        )

    def chaves_de_bloco(self) -> set[str]:
        """Chaves baratas que colocam candidatos no mesmo balde.

        Um contato entra em vários blocos de propósito: se o telefone foi
        digitado errado, o e-mail ainda o encontra.
        """
        chaves = {f"tel:{t}" for t in self.telefones}
        chaves |= {f"mail:{e}" for e in self.emails}
        if self.documento:
            chaves.add(f"doc:{self.documento}")
        fon = fonetico_br(self.nome)
        if fon:
            chaves.add(f"fon:{fon}")
        return chaves


#: Pesos dos sinais. Documento e e-mail sozinhos já bastam; nome nunca basta.
PESOS = {"documento": 1.0, "email": 0.9, "telefone": 0.6, "nome": 0.35, "empresa": 0.15}


def pontuar(a: Contato, b: Contato) -> tuple[float, list[str]]:
    """Pontua de 0 a 1 a chance de ``a`` e ``b`` serem a mesma pessoa.

    Returns:
        A pontuação e a lista de sinais que a sustentam. Devolver o *porquê*
        junto não é enfeite: quem revisa a fusão precisa ver o motivo, e um
        número solto não convence ninguém a apertar o botão.
    """
    sinais: list[str] = []
    pontos = 0.0

    if a.documento and a.documento == b.documento:
        pontos += PESOS["documento"]
        sinais.append("mesmo CPF/CNPJ")
    if a.emails & b.emails:
        pontos += PESOS["email"]
        sinais.append(f"mesmo e-mail ({next(iter(a.emails & b.emails))})")
    if a.telefones & b.telefones:
        pontos += PESOS["telefone"]
        sinais.append("mesmo telefone")

    tokens_a, tokens_b = tokens_significativos(a.nome), tokens_significativos(b.nome)
    if tokens_a and tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
        if jaccard >= 0.5:
            pontos += PESOS["nome"] * jaccard
            sinais.append(f"nome parecido ({jaccard:.0%})")
        elif fonetico_br(a.nome) == fonetico_br(b.nome):
            pontos += PESOS["nome"] * 0.8
            sinais.append("nome foneticamente igual")

    if a.empresa and b.empresa and tokens_significativos(a.empresa) == tokens_significativos(
        b.empresa
    ):
        pontos += PESOS["empresa"]
        sinais.append("mesma empresa")

    return min(1.0, pontos), sinais


@dataclass
class Par:
    """Dois contatos candidatos a fusão, com a evidência."""

    manter: Contato
    remover: Contato
    pontuacao: float
    sinais: list[str]

    def linha_csv(self) -> list[str]:
        return [
            str(self.manter.id),
            self.manter.nome,
            str(self.remover.id),
            self.remover.nome,
            f"{self.pontuacao:.2f}",
            "; ".join(self.sinais),
        ]


def escolher_sobrevivente(a: Contato, b: Contato) -> tuple[Contato, Contato]:
    """Decide qual contato fica, em critério estável e explicável.

    A ordem é: mais campos preenchidos, depois mais antigo, depois menor ID.
    O critério de desempate por ID existe para o resultado ser **determinístico** —
    rodar o robô duas vezes na mesma base tem que dar a mesma decisão, senão
    a revisão de ontem não vale hoje.
    """
    chave = lambda c: (-c.campos_preenchidos, c.criado_em or "9999", c.id)  # noqa: E731
    manter, remover = sorted([a, b], key=chave)
    return manter, remover


def encontrar_pares(contatos: list[Contato], limiar: float = 0.75) -> list[Par]:
    """Devolve os pares acima do limiar, do mais provável para o menos.

    Args:
        contatos: base achatada.
        limiar: pontuação mínima. ``0.75`` exige mais que só nome parecido.

    Returns:
        Pares ordenados por pontuação decrescente, sem repetição.
    """
    blocos: dict[str, list[Contato]] = defaultdict(list)
    for contato in contatos:
        for chave in contato.chaves_de_bloco():
            blocos[chave].append(contato)

    vistos: set[tuple[int, int]] = set()
    pares: list[Par] = []

    for candidatos in blocos.values():
        # Bloco gigante quase sempre e chave degenerada (telefone 00000000).
        # Comparar tudo ali dentro custa caro e nao encontra duplicata real.
        if len(candidatos) > 50:
            continue
        for i, a in enumerate(candidatos):
            for b in candidatos[i + 1 :]:
                dupla = (min(a.id, b.id), max(a.id, b.id))
                if dupla in vistos:
                    continue
                vistos.add(dupla)
                pontos, sinais = pontuar(a, b)
                if pontos >= limiar:
                    manter, remover = escolher_sobrevivente(a, b)
                    pares.append(Par(manter, remover, pontos, sinais))

    return sorted(pares, key=lambda p: p.pontuacao, reverse=True)
