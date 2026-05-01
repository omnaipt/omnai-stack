"""Lista de concorrentes por empresa.

Usada pelo worker Rita analise-concorrencia-semanal. O scraping faz hash
do conteudo principal para detectar paginas novas / alteradas entre scans.

Formato de cada concorrente:
    id: str               # slug
    nome: str             # nome comercial
    website: str          # URL base
    paginas_chave: list   # paginas relevantes para monitorizar (produtos, blog, precos)
    notas: str            # nota de posicionamento
"""
from __future__ import annotations

from typing import TypedDict


class Concorrente(TypedDict):
    id: str
    nome: str
    website: str
    paginas_chave: list[str]
    notas: str


OMNAI_CONCORRENTES: list[Concorrente] = [
    {
        "id": "kinsta-pt-ai",
        "nome": "Kinsta (consultoria AI PT)",
        "website": "https://kinsta.com/pt/",
        "paginas_chave": ["/pt/blog/", "/pt/ai/"],
        "notas": "Posicionamento tecnologico, escala internacional. Competidor em termos de percepcao de mercado.",
    },
    {
        "id": "priberam-ai",
        "nome": "Priberam Labs",
        "website": "https://www.priberam.com",
        "paginas_chave": ["/labs/", "/produtos"],
        "notas": "NLP portugues, ferramentas linguisticas. Competidor em NLP/IA PT.",
    },
    {
        "id": "unbabel",
        "nome": "Unbabel",
        "website": "https://unbabel.com",
        "paginas_chave": ["/blog/", "/solutions/"],
        "notas": "Translation-as-a-service. Competidor em IA aplicada B2B.",
    },
]


PREVINSA_CONCORRENTES: list[Concorrente] = [
    {
        "id": "prosegur",
        "nome": "Prosegur Portugal",
        "website": "https://www.prosegur.pt",
        "paginas_chave": ["/noticias", "/servicos"],
        "notas": "Grande player internacional. Monitorizar noticias e novos servicos.",
    },
    {
        "id": "securitas",
        "nome": "Securitas Portugal",
        "website": "https://www.securitas.pt",
        "paginas_chave": ["/novidades", "/servicos"],
        "notas": "Concorrente principal em vigilancia humana.",
    },
    {
        "id": "eps",
        "nome": "EPS Grupo",
        "website": "https://www.eps.pt",
        "paginas_chave": ["/noticias", "/servicos"],
        "notas": "Player portugues significativo em seguranca privada.",
    },
]


JMSOARES_CONCORRENTES: list[Concorrente] = [
    {
        "id": "jofebar",
        "nome": "Jofebar",
        "website": "https://www.jofebar.com",
        "paginas_chave": ["/noticias", "/obras"],
        "notas": "Construcao civil e obras publicas. Concursos sobrepoem-se.",
    },
    {
        "id": "mota-engil",
        "nome": "Mota-Engil",
        "website": "https://www.mota-engil.com",
        "paginas_chave": ["/media/", "/projectos"],
        "notas": "Gigante nacional. Monitorizar projectos mesmo que fora do alcance directo.",
    },
    {
        "id": "teixeira-duarte",
        "nome": "Teixeira Duarte",
        "website": "https://www.teixeiraduarte.pt",
        "paginas_chave": ["/noticias", "/obras"],
        "notas": "Construcao e engenharia. Concorre em alguns concursos de grande porte.",
    },
]


def concorrentes_por_empresa() -> dict[str, list[Concorrente]]:
    return {
        "OMNAI": OMNAI_CONCORRENTES,
        "Previnsa": PREVINSA_CONCORRENTES,
        "JMSoares": JMSOARES_CONCORRENTES,
    }


def todos() -> list[tuple[str, Concorrente]]:
    """Devolve lista de (empresa, concorrente) para iteracao plana."""
    out: list[tuple[str, Concorrente]] = []
    for empresa, lista in concorrentes_por_empresa().items():
        for c in lista:
            out.append((empresa, c))
    return out
