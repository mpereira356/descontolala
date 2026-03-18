#!/usr/bin/env python3
"""
Bot para encontrar produtos abaixo de um valor-alvo em páginas da Lacoste.

Uso:
  python bot_lacoste.py --url "https://www.lacoste.com/br/lacoste/masculino/vestu%C3%A1rio/" --max-preco 200
  python bot_lacoste.py --engine selenium --max-preco 200 --min-desconto 20
  python bot_lacoste.py --engine auto --json --mostrar-maior-desconto
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_URL = "https://www.lacoste.com/br/lacoste/masculino/vestu%C3%A1rio/"
PRECO_RE = re.compile(r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|[0-9]+(?:,[0-9]{2})?)")
DEFAULT_STATE_FILE = "lacoste_seen_products.json"


@dataclass
class Produto:
    nome: str
    preco: Decimal
    link: str
    preco_original: Decimal | None = None
    desconto_percentual: Decimal = Decimal("0")

    def chave_monitoramento(self) -> str:
        return self.link or self.nome


def _str_para_decimal_br(bruto: str) -> Decimal | None:
    normalizado = bruto.replace(".", "").replace(",", ".")
    try:
        return Decimal(normalizado)
    except InvalidOperation:
        return None


def extrair_precos_br(texto: str) -> list[Decimal]:
    valores: list[Decimal] = []
    for m in PRECO_RE.finditer(texto):
        valor = _str_para_decimal_br(m.group(1))
        if valor is not None:
            valores.append(valor)
    return valores


def inferir_preco_e_desconto(precos: list[Decimal]) -> tuple[Decimal | None, Decimal | None, Decimal]:
    if not precos:
        return None, None, Decimal("0")

    # Em geral cards com desconto têm dois preços: de/por.
    preco_atual = min(precos)
    preco_original = max(precos)
    if preco_original <= preco_atual:
        return preco_atual, None, Decimal("0")

    desconto = ((preco_original - preco_atual) / preco_original) * Decimal("100")
    return preco_atual, preco_original, desconto.quantize(Decimal("0.01"))


def baixar_html_requests(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.lacoste.com/",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def baixar_html_selenium(url: str, wait_s: float = 6.0, scroll_passes: int = 4) -> str:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.common.exceptions import TimeoutException, WebDriverException
    except Exception as exc:
        raise RuntimeError(
            "Selenium não está instalado. Rode: pip install selenium webdriver-manager"
        ) from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-background-networking")
    options.add_argument("--window-size=1440,2400")
    options.add_argument("--lang=pt-BR")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )

    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as exc:
        raise RuntimeError(
            "Falha ao iniciar o Chrome/Chromedriver no servidor. "
            "Verifique se o Google Chrome/Chromium e o chromedriver estão instalados e compatíveis."
        ) from exc

    try:
        driver.set_page_load_timeout(90)
        driver.set_script_timeout(90)
        driver.get(url)
        time.sleep(wait_s)
        for _ in range(max(0, scroll_passes)):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
        return driver.page_source
    except TimeoutException as exc:
        raise RuntimeError(
            "O Chrome/Chromedriver excedeu o tempo limite ao carregar a página no servidor. "
            "Em VPS isso costuma ser falta de memória, problema em /dev/shm ou travamento do navegador headless."
        ) from exc
    except WebDriverException as exc:
        raise RuntimeError(
            "O Chrome/Chromedriver travou durante a coleta da página. "
            "Em servidor isso normalmente indica incompatibilidade entre chrome/chromedriver ou ambiente sem recursos suficientes."
        ) from exc
    finally:
        driver.quit()


def extrair_produtos(html: str, base_url: str) -> list[Produto]:
    soup = BeautifulSoup(html, "html.parser")
    produtos: dict[tuple[str, str], Produto] = {}

    for card in soup.select("article, .product, .product-tile, .product-card, [data-testid*='product']"):
        texto = " ".join(card.stripped_strings)
        precos = extrair_precos_br(texto)
        preco_atual, preco_original, desconto = inferir_preco_e_desconto(precos)
        if preco_atual is None:
            continue

        nome_el = card.select_one("h2, h3, .name, .product-name, [data-testid*='name']")
        link_el = card.select_one("a[href]")
        if not nome_el or not link_el:
            continue

        nome = nome_el.get_text(" ", strip=True)
        link_abs = urljoin(base_url, link_el.get("href", ""))
        chave = (nome, link_abs)
        produtos[chave] = Produto(
            nome=nome,
            preco=preco_atual,
            link=link_abs,
            preco_original=preco_original,
            desconto_percentual=desconto,
        )

    return list(produtos.values())


def filtrar_produtos(
    produtos: Iterable[Produto],
    max_preco: Decimal | None,
    min_desconto: Decimal,
    apenas_com_desconto: bool,
) -> list[Produto]:
    resultado: list[Produto] = []
    for p in produtos:
        if max_preco is not None and p.preco > max_preco:
            continue
        if apenas_com_desconto and p.desconto_percentual <= 0:
            continue
        if p.desconto_percentual < min_desconto:
            continue
        resultado.append(p)

    return sorted(resultado, key=lambda x: (x.preco, -x.desconto_percentual))


def maior_desconto(produtos: Iterable[Produto]) -> Produto | None:
    com_desconto = [p for p in produtos if p.desconto_percentual > 0]
    if not com_desconto:
        return None
    return max(com_desconto, key=lambda x: x.desconto_percentual)


def imprimir(produtos: list[Produto], destaque: Produto | None = None) -> None:
    if destaque is not None:
        print(
            "MAIOR DESCONTO: "
            f"{destaque.desconto_percentual:.2f}% | "
            f"R$ {destaque.preco:.2f}"
            + (f" (de R$ {destaque.preco_original:.2f})" if destaque.preco_original else "")
            + f" | {destaque.nome} | {destaque.link}"
        )

    if not produtos:
        print("Nenhum produto encontrado com os filtros informados.")
        return

    for p in produtos:
        linha = f"R$ {p.preco:.2f}"
        if p.preco_original is not None:
            linha += f" (de R$ {p.preco_original:.2f}, -{p.desconto_percentual:.2f}%)"
        linha += f" | {p.nome} | {p.link}"
        print(linha)


def carregar_html(url: str, engine: str) -> str:
    if engine == "requests":
        return baixar_html_requests(url)
    if engine == "selenium":
        return baixar_html_selenium(url)

    try:
        return baixar_html_requests(url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (403, 429):
            return baixar_html_selenium(url)
        raise


def carregar_estado(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()

    if isinstance(data, list):
        return {str(item) for item in data}
    return set()


def salvar_estado(path: str, chaves: set[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(chaves), f, ensure_ascii=False, indent=2)


def formatar_produto_telegram(produto: Produto) -> str:
    preco = f"R$ {produto.preco:.2f}"
    if produto.preco_original is not None:
        preco += f" (de R$ {produto.preco_original:.2f}, -{produto.desconto_percentual:.2f}%)"
    return "\n".join(
        [
            "Novo produto com desconto encontrado",
            produto.nome,
            preco,
            produto.link,
        ]
    )


def enviar_telegram(token: str, chat_id: str, mensagem: str, timeout: int = 30) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": mensagem, "disable_web_page_preview": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Falha ao enviar mensagem para o Telegram: {payload}")


def executar_varredura(
    url: str,
    engine: str,
    max_preco: Decimal | None,
    min_desconto: Decimal,
    apenas_com_desconto: bool,
    mostrar_maior_desconto: bool,
) -> tuple[list[Produto], Produto | None]:
    html = carregar_html(url, engine)
    produtos = extrair_produtos(html, url)
    encontrados = filtrar_produtos(produtos, max_preco, min_desconto, apenas_com_desconto)
    destaque = maior_desconto(produtos) if mostrar_maior_desconto else None
    return encontrados, destaque


def monitorar(args: argparse.Namespace, max_preco: Decimal | None) -> int:
    token = args.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = args.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "Modo monitor exige credenciais do Telegram. Use --telegram-token e --telegram-chat-id "
            "ou defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )
        return 1

    apenas_com_desconto = True if args.monitorar_apenas_descontos else args.apenas_com_desconto
    if not args.monitorar_apenas_descontos and not args.apenas_com_desconto:
        apenas_com_desconto = True

    conhecidos = carregar_estado(args.state_file)
    if not conhecidos and args.inicializar_estado:
        try:
            encontrados, _ = executar_varredura(
                args.url,
                args.engine,
                max_preco,
                args.min_desconto,
                apenas_com_desconto,
                args.mostrar_maior_desconto,
            )
        except requests.RequestException as e:
            print(f"Erro de rede ao inicializar estado: {e}", file=sys.stderr)
            return 1
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Erro ao inicializar estado: {e}", file=sys.stderr)
            return 1

        conhecidos = {p.chave_monitoramento() for p in encontrados}
        salvar_estado(args.state_file, conhecidos)
        print(f"Estado inicial criado com {len(conhecidos)} produto(s).")

    print(f"Monitorando {args.url} a cada {args.intervalo} segundo(s).")
    while True:
        try:
            encontrados, _ = executar_varredura(
                args.url,
                args.engine,
                max_preco,
                args.min_desconto,
                apenas_com_desconto,
                args.mostrar_maior_desconto,
            )
            novos = [p for p in encontrados if p.chave_monitoramento() not in conhecidos]
            for produto in novos:
                enviar_telegram(token, chat_id, formatar_produto_telegram(produto))
                conhecidos.add(produto.chave_monitoramento())
                print(f"Alerta enviado: {produto.nome}")

            salvar_estado(args.state_file, conhecidos)
        except KeyboardInterrupt:
            print("\nMonitoramento interrompido pelo usuário.")
            return 0
        except requests.RequestException as e:
            print(f"Erro de rede durante monitoramento: {e}", file=sys.stderr)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
        except Exception as e:
            print(f"Erro no monitoramento: {e}", file=sys.stderr)

        time.sleep(args.intervalo)


def main() -> int:
    parser = argparse.ArgumentParser(description="Encontra produtos da Lacoste com filtros de preço e desconto.")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL da categoria")
    parser.add_argument(
        "--max-preco",
        type=Decimal,
        default=Decimal("200"),
        help="Preço máximo em reais (use -1 para desativar)",
    )
    parser.add_argument("--min-desconto", type=Decimal, default=Decimal("0"), help="Desconto mínimo em %%")
    parser.add_argument("--apenas-com-desconto", action="store_true", help="Mostra só itens com desconto")
    parser.add_argument("--mostrar-maior-desconto", action="store_true", help="Mostra o maior desconto encontrado")
    parser.add_argument("--engine", choices=["auto", "requests", "selenium"], default="auto")
    parser.add_argument("--json", action="store_true", help="Saída em JSON")
    parser.add_argument("--monitor", action="store_true", help="Mantém o script em execução e envia avisos no Telegram")
    parser.add_argument("--intervalo", type=int, default=300, help="Intervalo entre varreduras no modo monitor")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Arquivo JSON com produtos já avisados")
    parser.add_argument("--inicializar-estado", action="store_true", help="Grava os itens atuais no estado sem alertar")
    parser.add_argument("--monitorar-apenas-descontos", action="store_true", help="No modo monitor, alerta somente itens com desconto")
    parser.add_argument("--telegram-token", default="", help="Token do bot do Telegram")
    parser.add_argument("--telegram-chat-id", default="", help="Chat ID do Telegram")
    args = parser.parse_args()

    max_preco = None if args.max_preco < 0 else args.max_preco

    if args.monitor:
        return monitorar(args, max_preco)

    try:
        encontrados, destaque = executar_varredura(
            args.url,
            args.engine,
            max_preco,
            args.min_desconto,
            args.apenas_com_desconto,
            args.mostrar_maior_desconto,
        )
    except requests.RequestException as e:
        print(f"Erro de rede ao acessar a página: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Erro ao processar dados: {e}", file=sys.stderr)
        return 1

    if args.json:
        payload = []
        for p in encontrados:
            item = {
                **asdict(p),
                "preco": f"{p.preco:.2f}",
                "desconto_percentual": f"{p.desconto_percentual:.2f}",
                "preco_original": f"{p.preco_original:.2f}" if p.preco_original is not None else None,
            }
            payload.append(item)

        output = {"produtos": payload}
        if destaque is not None:
            output["maior_desconto"] = {
                **asdict(destaque),
                "preco": f"{destaque.preco:.2f}",
                "desconto_percentual": f"{destaque.desconto_percentual:.2f}",
                "preco_original": f"{destaque.preco_original:.2f}" if destaque.preco_original is not None else None,
            }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        imprimir(encontrados, destaque=destaque)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
