"""Testes do pareamento — precisão importa mais que recall aqui.

Fundir dois contatos diferentes é irreversível e destrói histórico. Deixar
uma duplicata passar custa uma segunda rodada. Os testes abaixo protegem
principalmente contra o primeiro erro.
"""

from __future__ import annotations

import pytest

from dedup.matching import (
    Contato,
    encontrar_pares,
    escolher_sobrevivente,
    fonetico_br,
    pontuar,
    tokens_significativos,
)


def contato(id_, nome="", emails=(), telefones=(), empresa="", doc="", criado="", campos=5):
    return Contato(
        id=id_,
        nome=nome,
        emails=frozenset(emails),
        telefones=frozenset(telefones),
        empresa=empresa,
        documento=doc,
        criado_em=criado,
        campos_preenchidos=campos,
    )


class TestFonetico:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("Gonçalves", "Goncalvez"),
            ("Souza", "Sousa"),
            ("Xavier", "Chavier"),
            ("Anna", "Ana"),
            ("Luiz", "Luis"),
            ("Teixeira", "Teicheira"),
            ("Filipe", "Philipe"),
        ],
    )
    def test_variacoes_comuns_colidem(self, a, b):
        assert fonetico_br(a) == fonetico_br(b), f"{a} e {b} deveriam colidir"

    @pytest.mark.parametrize("a,b", [("Silva", "Souza"), ("Pereira", "Ferreira"), ("Ana", "Bruno")])
    def test_nomes_diferentes_nao_colidem(self, a, b):
        assert fonetico_br(a) != fonetico_br(b)

    def test_string_vazia_nao_quebra(self):
        assert fonetico_br("") == ""


class TestTokens:
    def test_remove_ruido_societario(self):
        assert tokens_significativos("Aguia Comercio LTDA") == {"aguia"}

    def test_ignora_acento_e_pontuacao(self):
        assert tokens_significativos("Construções Àguia!") == {"construcoes", "aguia"}


class TestPontuacao:
    def test_mesmo_documento_ja_e_quase_certeza(self):
        pontos, sinais = pontuar(
            contato(1, "Joao Silva", doc="52998224725"),
            contato(2, "J. Silva", doc="52998224725"),
        )
        assert pontos >= 0.9
        assert "mesmo CPF/CNPJ" in sinais

    def test_mesmo_email_basta(self):
        pontos, _ = pontuar(
            contato(1, "Joao", emails={"j@x.com"}),
            contato(2, "Joao Silva", emails={"j@x.com"}),
        )
        assert pontos >= 0.75

    def test_so_nome_parecido_nao_basta(self):
        """Homônimo é comum: 'Ana Silva' e 'Ana Silva' podem ser duas pessoas."""
        pontos, _ = pontuar(contato(1, "Ana Silva"), contato(2, "Ana Silva"))
        assert pontos < 0.75, f"pontuou {pontos:.2f}, alto demais para so o nome"

    def test_nome_mais_telefone_basta(self):
        pontos, sinais = pontuar(
            contato(1, "Ana Silva", telefones={"98765432"}),
            contato(2, "Ana Silva", telefones={"98765432"}),
        )
        assert pontos >= 0.75
        assert "mesmo telefone" in sinais

    def test_pessoas_distintas_no_mesmo_telefone_corporativo(self):
        """Marido e esposa, ou dois sócios: mesmo telefone, gente diferente."""
        pontos, _ = pontuar(
            contato(1, "Ana Silva", telefones={"34567890"}),
            contato(2, "Bruno Costa", telefones={"34567890"}),
        )
        assert pontos < 0.75

    def test_sinais_explicam_a_pontuacao(self):
        _, sinais = pontuar(
            contato(1, "Ana Silva", emails={"a@x.com"}, telefones={"98765432"}),
            contato(2, "Ana Silva", emails={"a@x.com"}, telefones={"98765432"}),
        )
        assert len(sinais) >= 3, "quem revisa precisa ver o porque"

    def test_pontuacao_nunca_passa_de_um(self):
        forte = contato(1, "Ana Silva", emails={"a@x.com"}, telefones={"1"}, doc="52998224725",
                        empresa="Aguia")
        outro = contato(2, "Ana Silva", emails={"a@x.com"}, telefones={"1"}, doc="52998224725",
                        empresa="Aguia")
        assert pontuar(forte, outro)[0] == 1.0


class TestSobrevivente:
    def test_fica_o_mais_completo(self):
        rico = contato(2, "Ana Silva", campos=12)
        pobre = contato(1, "Ana", campos=4)
        manter, remover = escolher_sobrevivente(pobre, rico)
        assert manter.id == 2 and remover.id == 1

    def test_empate_de_campos_fica_o_mais_antigo(self):
        novo = contato(2, "Ana", criado="2026-08-01", campos=5)
        velho = contato(1, "Ana", criado="2024-01-01", campos=5)
        manter, _ = escolher_sobrevivente(novo, velho)
        assert manter.id == 1

    def test_decisao_e_deterministica(self):
        """Rodar duas vezes na mesma base tem que dar a mesma decisao."""
        a, b = contato(7, "Ana", campos=5, criado="2026-01-01"), contato(3, "Ana", campos=5,
                                                                        criado="2026-01-01")
        assert escolher_sobrevivente(a, b) == escolher_sobrevivente(b, a)


class TestEncontrarPares:
    def test_acha_duplicata_obvia(self):
        base = [
            contato(1, "Joao Silva", emails={"joao@x.com"}, campos=8),
            contato(2, "Joao da Silva", emails={"joao@x.com"}, campos=5),
            contato(3, "Maria Souza", emails={"maria@y.com"}),
        ]
        pares = encontrar_pares(base)
        assert len(pares) == 1
        assert {pares[0].manter.id, pares[0].remover.id} == {1, 2}

    def test_nao_repete_o_mesmo_par(self):
        """O contato entra em varios blocos; o par nao pode sair duplicado."""
        base = [
            contato(1, "Ana Silva", emails={"a@x.com"}, telefones={"98765432"}, doc="52998224725"),
            contato(2, "Ana Silva", emails={"a@x.com"}, telefones={"98765432"}, doc="52998224725"),
        ]
        assert len(encontrar_pares(base)) == 1

    def test_ordena_do_mais_provavel_para_o_menos(self):
        base = [
            contato(1, "Ana Silva", emails={"a@x.com"}, doc="52998224725"),
            contato(2, "Ana Silva", emails={"a@x.com"}, doc="52998224725"),
            contato(3, "Bruno Costa", telefones={"11112222"}),
            contato(4, "Bruno Costa", telefones={"11112222"}),
        ]
        pares = encontrar_pares(base)
        assert pares[0].pontuacao >= pares[-1].pontuacao

    def test_bloco_degenerado_e_descartado(self):
        """60 contatos com o telefone 00000000 nao devem virar 1770 comparacoes."""
        base = [contato(i, f"Pessoa {i}", telefones={"00000000"}) for i in range(60)]
        assert encontrar_pares(base) == []

    def test_base_sem_duplicata_devolve_lista_vazia(self):
        base = [contato(i, f"Pessoa {i}", emails={f"p{i}@x.com"}) for i in range(20)]
        assert encontrar_pares(base) == []


class TestContatoDoCRM:
    def test_achata_listas_de_comunicacao(self):
        bruto = {
            "ID": "42",
            "NAME": "Ana",
            "LAST_NAME": "Silva",
            "EMAIL": [{"VALUE": "Ana@X.com"}, {"VALUE": "ana2@x.com"}],
            "PHONE": [{"VALUE": "+55 (11) 98765-4321"}],
            "DATE_CREATE": "2026-01-01T10:00:00+03:00",
        }
        c = Contato.do_crm(bruto)
        assert c.id == 42
        assert c.nome == "Ana Silva"
        assert c.emails == {"ana@x.com", "ana2@x.com"}, "e-mail normalizado em minusculo"
        assert c.telefones == {"87654321"}, "guarda os 8 finais, ignorando DDI e DDD"

    def test_contato_sem_comunicacao_nao_quebra(self):
        c = Contato.do_crm({"ID": "1", "NAME": "Ana", "EMAIL": None, "PHONE": []})
        assert c.emails == frozenset() and c.telefones == frozenset()

    def test_telefone_curto_demais_e_descartado(self):
        c = Contato.do_crm({"ID": "1", "PHONE": [{"VALUE": "1234"}]})
        assert c.telefones == frozenset()
