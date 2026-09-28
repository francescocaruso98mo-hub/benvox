#!/usr/bin/env python3
"""
MARCO - agente di ricerca benefit aziendali per Benevox.

Pipeline:
  1. Ricerca web sui benefit dell'azienda (DuckDuckGo + download pagine con requests)
  2. Estrazione dati strutturati con Groq (llama-3.3-70b-versatile, output JSON validato)
  3. Salvataggio su Supabase (tabelle `aziende` e `benefit`)
  4. Report a terminale + file JSON in ./reports

Uso:
  python marco.py                         # Ferrari, Maranello
  python marco.py "Barilla" --sede Parma
  python marco.py "Ferrari" --dry-run     # non scrive su Supabase
  python marco.py "Ferrari" --url https://...   # aggiunge pagine da leggere

Variabili d'ambiente (vedi .env.example):
  GROQ_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qs, urlparse

import groq
import requests
from pydantic import BaseModel, Field, ValidationError
from supabase import Client, create_client

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

MODEL = "llama-3.3-70b-versatile"

CATEGORIE = ("smart_working", "mensa", "welfare", "sanita", "premi", "sconti", "altro")
Categoria = Literal["smart_working", "mensa", "welfare", "sanita", "premi", "sconti", "altro"]

ICONE = {
    "smart_working": "🏠",
    "mensa": "🍽️",
    "welfare": "🎁",
    "sanita": "🩺",
    "premi": "💰",
    "sconti": "🏷️",
    "altro": "✨",
}

REPORT_DIR = Path(__file__).with_name("reports")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
}

# Limiti pensati per il piano gratuito Groq (llama-3.3-70b: ~12k token/minuto).
RISULTATI_PER_QUERY = 4
MAX_PAGINE = 10
MAX_CARATTERI_PAGINA = 2500
MAX_CARATTERI_TOTALI = 20000

QUERY = [
    "{azienda} {sede} benefit dipendenti",
    "{azienda} welfare aziendale dipendenti",
    "{azienda} premio di competitività contratto integrativo",
    "{azienda} smart working dipendenti",
    "{azienda} mensa aziendale assistenza sanitaria dipendenti",
    "{azienda} sconti convenzioni dipendenti",
    "{azienda} recensioni dipendenti benefit glassdoor",
]

PAROLE_CHIAVE = re.compile(
    r"benefit|welfare|smart.?working|lavoro (agile|ibrido|da remoto)|remote|ibrid|"
    r"mensa|pasto|pasti|ticket|buon[io] pasto|sanit|salute|medic|check.?up|assicuraz|"
    r"premio|premi |bonus|competitivit|integrativ|retribu|stipend|aument|"
    r"sconto|sconti|convenzion|asilo|nido|borse di studio|formazione|palestra|fitness|"
    r"dipendenti|lavoratori|employees|headcount|perk|canteen|health|discount|flexib",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Modelli dati
# --------------------------------------------------------------------------- #
class Benefit(BaseModel):
    nome: str
    descrizione: str
    categoria: Categoria
    affidabilita: Literal["alta", "media", "bassa"]
    fonti: list[str] = Field(default_factory=list)


class ProfiloAzienda(BaseModel):
    nome: str
    settore: Optional[str] = None
    sede_principale: Optional[str] = None
    headcount: Optional[int] = None
    benefit: list[Benefit]
    note: Optional[str] = None


class Pagina(BaseModel):
    url: str
    titolo: str
    testo: str


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def env(name: str, required: bool = True) -> Optional[str]:
    value = os.environ.get(name)
    if required and not value:
        sys.exit(f"[MARCO] Variabile d'ambiente mancante: {name} (vedi .env.example)")
    return value


class _EstrattoreTesto(HTMLParser):
    """Estrae titolo e testo visibile da una pagina HTML (solo libreria standard)."""

    IGNORA = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}
    BLOCCHI = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self._ignora = 0
        self._in_title = False
        self.titolo = ""
        self._parti: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORA:
            self._ignora += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCCHI:
            self._parti.append("\n")

    def handle_endtag(self, tag):
        if tag in self.IGNORA and self._ignora:
            self._ignora -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCCHI:
            self._parti.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.titolo += data
        elif not self._ignora:
            self._parti.append(data)

    def testo(self) -> str:
        righe = (re.sub(r"\s+", " ", r).strip() for r in "".join(self._parti).split("\n"))
        return "\n".join(r for r in righe if len(r) > 25)


def estratti_rilevanti(testo: str, limite: int) -> str:
    """Tiene solo le righe che parlano di benefit, per stare nei limiti di token."""
    righe = testo.split("\n")
    scelte = [r for r in righe if PAROLE_CHIAVE.search(r)]
    return "\n".join(scelte)[:limite]


# --------------------------------------------------------------------------- #
# 1. Ricerca web
# --------------------------------------------------------------------------- #
def cerca_duckduckgo(query: str, n: int) -> list[str]:
    r = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query, "kl": "it-it"},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    urls = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
        href = href.replace("&amp;", "&")
        if "duckduckgo.com/l/" in href:  # link di redirect: l'URL vero è nel parametro uddg
            href = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        if href.startswith("http") and "duckduckgo.com" not in href:
            urls.append(href)
        if len(urls) >= n:
            break
    return urls


def scarica_pagina(url: str) -> Optional[Pagina]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException:
        return None
    if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", ""):
        return None
    r.encoding = r.encoding or r.apparent_encoding
    parser = _EstrattoreTesto()
    try:
        parser.feed(r.text)
    except Exception:
        return None
    testo = estratti_rilevanti(parser.testo(), MAX_CARATTERI_PAGINA)
    if len(testo) < 200:
        return None
    return Pagina(url=r.url, titolo=re.sub(r"\s+", " ", parser.titolo).strip()[:150], testo=testo)


def ricerca_web(azienda: str, sede: Optional[str], url_extra: list[str]) -> list[Pagina]:
    candidati: list[str] = list(url_extra)
    for q in QUERY:
        query = q.format(azienda=azienda, sede=sede or "").replace("  ", " ")
        try:
            trovati = cerca_duckduckgo(query, RISULTATI_PER_QUERY)
        except requests.RequestException as e:
            print(f"[MARCO]     ricerca fallita per «{query}»: {e}")
            continue
        print(f"[MARCO]     «{query}» → {len(trovati)} risultati")
        candidati.extend(trovati)
        time.sleep(1.5)  # evita di essere bloccati da DuckDuckGo

    pagine: list[Pagina] = []
    totale = 0
    for url in dict.fromkeys(candidati):
        if len(pagine) >= MAX_PAGINE or totale >= MAX_CARATTERI_TOTALI:
            break
        pagina = scarica_pagina(url)
        if pagina is None:
            continue
        pagina.testo = pagina.testo[: MAX_CARATTERI_TOTALI - totale]
        totale += len(pagina.testo)
        pagine.append(pagina)
        print(f"[MARCO]     letta: {urlparse(pagina.url).netloc} — {pagina.titolo[:60]}")
    return pagine


# --------------------------------------------------------------------------- #
# 2. Estrazione strutturata (Groq)
# --------------------------------------------------------------------------- #
PROMPT_SISTEMA = """Sei MARCO, analista di Benevox, piattaforma italiana che raccoglie \
informazioni REALI sui benefit aziendali. Rispondi SOLO con un oggetto JSON valido."""

PROMPT_ESTRAZIONE = """Dagli estratti di pagine web qui sotto, estrai i benefit per i \
dipendenti di "{azienda}"{sede_txt}.

Rispondi con un oggetto JSON con esattamente questa struttura:
{{
  "nome": "{azienda}",
  "settore": string o null,
  "sede_principale": string o null,
  "headcount": intero o null,
  "note": string o null,
  "benefit": [
    {{
      "nome": string,
      "descrizione": string,
      "categoria": "smart_working" | "mensa" | "welfare" | "sanita" | "premi" | "sconti" | "altro",
      "affidabilita": "alta" | "media" | "bassa",
      "fonti": [numeri delle pagine, es. 1, 3]
    }}
  ]
}}

Regole:
- Usa SOLO informazioni presenti negli estratti. Non inventare nulla: se non trovi \
un'area, non creare benefit per quell'area.
- Considera solo "{azienda}" e non altre aziende citate nelle pagine.
- Un elemento per ogni benefit distinto e concreto (niente frasi generiche come \
"ottimo ambiente di lavoro").
- "nome": breve, in italiano (max ~60 caratteri), es. "Premio di competitività".
- "descrizione": 1-3 frasi in italiano con i dettagli concreti (importi, giorni, \
condizioni, anno).
- "categoria": usa "altro" solo se non rientra nelle altre (es. formazione).
- "affidabilita": "alta" se da sito ufficiale, accordo sindacale o stampa autorevole; \
"media" se da una sola fonte secondaria; "bassa" se da recensioni anonime o dati datati.
- "fonti": i numeri [n] delle pagine da cui proviene l'informazione.
- "headcount": numero totale di dipendenti, solo se indicato.
- In "note" segnala informazioni in conflitto tra fonti o aree senza dati.

{pagine}"""


def formatta_pagine(pagine: list[Pagina]) -> str:
    return "\n\n".join(
        f"<pagina n=\"{i}\" url=\"{p.url}\" titolo=\"{p.titolo}\">\n{p.testo}\n</pagina>"
        for i, p in enumerate(pagine, 1)
    )


def _normalizza_fonti(dati: dict, pagine: list[Pagina]) -> dict:
    """Converte i numeri di pagina citati dal modello negli URL corrispondenti."""
    for b in dati.get("benefit") or []:
        urls = []
        for f in b.get("fonti") or []:
            s = str(f).strip().strip("[]")
            if s.isdigit() and 1 <= int(s) <= len(pagine):
                urls.append(pagine[int(s) - 1].url)
            elif s.startswith("http"):
                urls.append(s)
        b["fonti"] = list(dict.fromkeys(urls))
        if isinstance(b.get("categoria"), str):
            b["categoria"] = b["categoria"].strip().lower().replace("à", "a").replace(" ", "_")
    return dati


def estrai_dati(client: groq.Groq, azienda: str, sede: Optional[str], pagine: list[Pagina]) -> ProfiloAzienda:
    sede_txt = f" (sede: {sede})" if sede else ""
    messages = [
        {"role": "system", "content": PROMPT_SISTEMA},
        {"role": "user", "content": PROMPT_ESTRAZIONE.format(
            azienda=azienda, sede_txt=sede_txt, pagine=formatta_pagine(pagine))},
    ]

    for tentativo in range(2):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            max_completion_tokens=4096,
        )
        contenuto = response.choices[0].message.content or ""
        try:
            dati = _normalizza_fonti(json.loads(contenuto), pagine)
            return ProfiloAzienda.model_validate(dati)
        except (json.JSONDecodeError, ValidationError) as e:
            if tentativo == 1:
                sys.exit(f"[MARCO] JSON di estrazione non valido: {e}")
            # Secondo tentativo: rimanda al modello l'errore da correggere.
            messages += [
                {"role": "assistant", "content": contenuto},
                {"role": "user", "content": f"Il JSON non è valido: {e}. Correggilo e "
                                            "rispondi solo con il JSON completo."},
            ]
    raise AssertionError("irraggiungibile")


# --------------------------------------------------------------------------- #
# 3. Salvataggio Supabase
# --------------------------------------------------------------------------- #
def salva_su_supabase(db: Client, profilo: ProfiloAzienda, nome_azienda: str) -> dict:
    esito = {"azienda_id": None, "azienda_creata": False, "inseriti": [], "gia_presenti": []}

    campi = {
        k: v for k, v in {
            "settore": profilo.settore,
            "sede_principale": profilo.sede_principale,
            "headcount": profilo.headcount,
        }.items() if v is not None
    }

    esistente = db.table("aziende").select("*").ilike("nome", nome_azienda).limit(1).execute().data
    if esistente:
        azienda = esistente[0]
        # Completa solo i campi vuoti: non sovrascrive dati già curati a mano.
        da_aggiornare = {k: v for k, v in campi.items() if azienda.get(k) in (None, "")}
        if da_aggiornare:
            db.table("aziende").update(da_aggiornare).eq("id", azienda["id"]).execute()
    else:
        azienda = db.table("aziende").insert({"nome": nome_azienda, **campi}).execute().data[0]
        esito["azienda_creata"] = True
    esito["azienda_id"] = azienda["id"]

    presenti = db.table("benefit").select("nome").eq("azienda_id", azienda["id"]).execute().data
    nomi_presenti = {r["nome"].strip().lower() for r in presenti}

    nuovi = []
    for b in profilo.benefit:
        if b.nome.strip().lower() in nomi_presenti:
            esito["gia_presenti"].append(b.nome)
            continue
        nomi_presenti.add(b.nome.strip().lower())
        nuovi.append({
            "azienda_id": azienda["id"],
            "nome": b.nome,
            "descrizione": b.descrizione,
            "icona": ICONE[b.categoria],
            "categoria": b.categoria,
            # Dati raccolti dal web: restano da verificare (es. da dipendenti).
            "verificato": False,
        })
    if nuovi:
        db.table("benefit").insert(nuovi).execute()
        esito["inseriti"] = [n["nome"] for n in nuovi]
    return esito


# --------------------------------------------------------------------------- #
# 4. Report
# --------------------------------------------------------------------------- #
def stampa_report(profilo: ProfiloAzienda, pagine: list[Pagina], esito: Optional[dict]) -> None:
    linea = "=" * 72
    print(f"\n{linea}\n  MARCO · Report benefit — {profilo.nome}\n{linea}")
    print(f"  Settore:     {profilo.settore or '—'}")
    print(f"  Sede:        {profilo.sede_principale or '—'}")
    print(f"  Dipendenti:  {profilo.headcount or '—'}")
    print(f"  Benefit:     {len(profilo.benefit)} trovati")

    for cat in CATEGORIE:
        items = [b for b in profilo.benefit if b.categoria == cat]
        if not items:
            continue
        print(f"\n  {ICONE[cat]}  {cat.upper().replace('_', ' ')} ({len(items)})")
        for b in items:
            print(f"     • {b.nome}  [affidabilità: {b.affidabilita}]")
            print(f"       {b.descrizione}")
            for f in b.fonti:
                print(f"       ↳ {urlparse(f).netloc or f}")
            if not b.fonti:
                print("       ↳ ⚠ nessuna fonte indicata")

    mancanti = [c for c in CATEGORIE[:-1] if not any(b.categoria == c for b in profilo.benefit)]
    if mancanti:
        print(f"\n  Nessuna informazione trovata per: {', '.join(mancanti)}")
    if profilo.note:
        print(f"\n  Note: {profilo.note}")

    print(f"\n  Pagine lette: {len(pagine)}")
    for i, p in enumerate(pagine, 1):
        print(f"     [{i}] {p.url}")
    if esito is None:
        print("\n  [dry-run] Nessuna scrittura su Supabase.")
    else:
        stato = "creata" if esito["azienda_creata"] else "già presente"
        print(f"\n  Supabase: azienda {stato} (id={esito['azienda_id']})")
        print(f"            {len(esito['inseriti'])} benefit inseriti, "
              f"{len(esito['gia_presenti'])} già presenti (saltati)")
    print(linea)


def salva_report_json(profilo: ProfiloAzienda, pagine: list[Pagina], esito: Optional[dict]) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "_" for c in profilo.nome.lower()).strip("_")
    path = REPORT_DIR / f"{slug}_{ts}.json"
    path.write_text(json.dumps({
        "generato_il": ts,
        "modello": MODEL,
        "profilo": profilo.model_dump(),
        "pagine_lette": [p.model_dump() for p in pagine],
        "esito_supabase": esito,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="MARCO - ricerca benefit aziendali per Benevox")
    parser.add_argument("azienda", nargs="?", default="Ferrari", help="Nome dell'azienda (default: Ferrari)")
    parser.add_argument("--sede", default="Maranello", help="Sede per disambiguare la ricerca")
    parser.add_argument("--url", action="append", default=[], help="URL extra da leggere (ripetibile)")
    parser.add_argument("--dry-run", action="store_true", help="Non scrivere su Supabase")
    args = parser.parse_args()

    client = groq.Groq(api_key=env("GROQ_API_KEY"))
    db = None
    if not args.dry_run:
        db = create_client(env("SUPABASE_URL"), env("SUPABASE_SECRET_KEY"))

    print(f"[MARCO] 1/4 Ricerca web sui benefit di {args.azienda} ({args.sede})…")
    pagine = ricerca_web(args.azienda, args.sede, args.url)
    if not pagine:
        sys.exit("[MARCO] Nessuna pagina utile trovata. DuckDuckGo potrebbe aver limitato "
                 "le richieste: riprova più tardi o passa delle pagine con --url.")
    print(f"[MARCO]     {len(pagine)} pagine utili lette.")

    print(f"[MARCO] 2/4 Estrazione dati strutturati con Groq ({MODEL})…")
    profilo = estrai_dati(client, args.azienda, args.sede, pagine)
    print(f"[MARCO]     {len(profilo.benefit)} benefit estratti.")

    esito = None
    if db is not None:
        print("[MARCO] 3/4 Salvataggio su Supabase…")
        esito = salva_su_supabase(db, profilo, args.azienda)
    else:
        print("[MARCO] 3/4 Salvataggio saltato (--dry-run).")

    print("[MARCO] 4/4 Report")
    stampa_report(profilo, pagine, esito)
    print(f"[MARCO] Report completo salvato in {salva_report_json(profilo, pagine, esito)}")


if __name__ == "__main__":
    try:
        main()
    except groq.AuthenticationError:
        sys.exit("[MARCO] GROQ_API_KEY non valida.")
    except groq.RateLimitError:
        sys.exit("[MARCO] Limite di richieste Groq raggiunto: riprova tra qualche minuto.")
    except groq.APIStatusError as e:
        sys.exit(f"[MARCO] Errore API Groq {e.status_code}: {e.message}")
    except groq.APIConnectionError:
        sys.exit("[MARCO] Impossibile raggiungere l'API Groq (rete).")
