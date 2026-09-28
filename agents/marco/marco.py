#!/usr/bin/env python3
"""
MARCO - agente di ricerca benefit aziendali per Benevox.

Pipeline:
  1. Ricerca web sui benefit dell'azienda (tool web_search di Claude)
  2. Estrazione dati strutturati con Claude (output JSON validato)
  3. Salvataggio su Supabase (tabelle `aziende` e `benefit`)
  4. Report a terminale + file JSON in ./reports

Uso:
  python marco.py                         # Ferrari, Maranello
  python marco.py "Barilla" --sede Parma
  python marco.py "Ferrari" --dry-run     # non scrive su Supabase

Variabili d'ambiente (vedi .env.example):
  ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

import anthropic
import requests
from pydantic import BaseModel, Field, ValidationError
from supabase import Client, create_client

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

MODEL = "claude-opus-5-5"
# Instrada automaticamente la richiesta su un altro modello se quello
# principale la rifiuta per policy (vedi docs "refusals and fallback").
FALLBACK_BETA = "server-side-fallback-2026-07-01"

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


# Schema JSON per structured outputs (additionalProperties: false ovunque).
SCHEMA_ESTRAZIONE = {
    "type": "object",
    "properties": {
        "nome": {"type": "string"},
        "settore": {"type": ["string", "null"]},
        "sede_principale": {"type": ["string", "null"]},
        "headcount": {"type": ["integer", "null"]},
        "note": {"type": ["string", "null"]},
        "benefit": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "descrizione": {"type": "string"},
                    "categoria": {"type": "string", "enum": list(CATEGORIE)},
                    "affidabilita": {"type": "string", "enum": ["alta", "media", "bassa"]},
                    "fonti": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["nome", "descrizione", "categoria", "affidabilita", "fonti"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["nome", "settore", "sede_principale", "headcount", "note", "benefit"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def env(name: str, required: bool = True) -> Optional[str]:
    value = os.environ.get(name)
    if required and not value:
        sys.exit(f"[MARCO] Variabile d'ambiente mancante: {name} (vedi .env.example)")
    return value


def testo_da_risposta(response) -> str:
    """Concatena i blocchi di testo, ignorando thinking, tool e blocchi fallback."""
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def verifica_rifiuto(response, fase: str) -> None:
    if response.stop_reason == "refusal":
        dettagli = getattr(response, "stop_details", None)
        sys.exit(f"[MARCO] Claude ha rifiutato la richiesta in fase di {fase}: {dettagli}")


def url_raggiungibile(url: str) -> bool:
    """Controllo leggero che una fonte citata esista davvero (HEAD, poi GET)."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BenevoxMarco/1.0)"}
    try:
        r = requests.head(url, headers=headers, timeout=8, allow_redirects=True)
        if r.status_code in (403, 405) or r.status_code >= 500:
            r = requests.get(url, headers=headers, timeout=8, stream=True)
        return r.status_code < 400
    except requests.RequestException:
        return False


# --------------------------------------------------------------------------- #
# 1. Ricerca web
# --------------------------------------------------------------------------- #
PROMPT_RICERCA = """Sei MARCO, ricercatore di Benevox, piattaforma italiana che raccoglie \
informazioni REALI sui benefit aziendali.

Cerca sul web informazioni sui benefit per i dipendenti di "{azienda}"{sede_txt}.

Copri almeno queste aree:
- smart working / lavoro ibrido / flessibilità oraria
- mensa aziendale, buoni pasto
- welfare aziendale (piattaforme welfare, rimborsi, asili nido, borse di studio, trasporti)
- sanità integrativa, check-up, assistenza medica
- premi di risultato / competitività, bonus, aumenti legati al contratto integrativo
- sconti e convenzioni per i dipendenti (es. prodotti aziendali, palestre, negozi)

Fonti preferite: sito ufficiale e pagine carriere, bilanci / report di sostenibilità, \
comunicati stampa, accordi sindacali (contratto integrativo aziendale), testate \
giornalistiche affidabili, recensioni di dipendenti (Glassdoor, Indeed) come fonte \
secondaria. Preferisci informazioni recenti e indica l'anno quando disponibile.

Raccogli anche: settore, sede principale, numero di dipendenti.

Restituisci un resoconto dettagliato in italiano, organizzato per area. Per ogni \
informazione indica l'URL della fonte. Se due fonti sono in conflitto, riportale \
entrambe. Non inventare nulla: se un'area non è documentata, scrivilo."""


def ricerca_web(client: anthropic.Anthropic, azienda: str, sede: Optional[str]) -> tuple[str, list[str]]:
    sede_txt = f" (sede: {sede})" if sede else ""
    messages = [{"role": "user", "content": PROMPT_RICERCA.format(azienda=azienda, sede_txt=sede_txt)}]
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 15,
        "user_location": {"type": "approximate", "country": "IT", "timezone": "Europe/Rome"},
    }]

    fonti: list[str] = []
    for _ in range(6):  # continua i turni in pausa (pause_turn), con un tetto
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        verifica_rifiuto(response, "ricerca")
        for block in response.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                fonti.extend(r.url for r in block.content if getattr(r, "url", None))

        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})

    resoconto = testo_da_risposta(response)
    if not resoconto:
        sys.exit("[MARCO] La ricerca non ha prodotto alcun resoconto.")
    return resoconto, list(dict.fromkeys(fonti))


# --------------------------------------------------------------------------- #
# 2. Estrazione strutturata
# --------------------------------------------------------------------------- #
PROMPT_ESTRAZIONE = """Dal resoconto di ricerca qui sotto, estrai i benefit aziendali \
di "{azienda}" in forma strutturata per il database Benevox.

Regole:
- Un elemento per ogni benefit distinto e concreto (niente frasi generiche come \
"ottimo ambiente di lavoro").
- `nome`: breve, in italiano (max ~60 caratteri), es. "Premio di competitività".
- `descrizione`: 1-3 frasi con i dettagli concreti (importi, giorni, condizioni, anno).
- `categoria`: una tra smart_working, mensa, welfare, sanita, premi, sconti; usa \
"altro" solo se non rientra in nessuna (es. formazione, mobilità interna).
- `affidabilita`: "alta" se da fonte ufficiale / accordo sindacale / stampa \
autorevole; "media" se da una sola fonte secondaria; "bassa" se da recensioni \
anonime o informazioni datate.
- `fonti`: gli URL citati nel resoconto per quel benefit.
- Compila settore, sede_principale, headcount (intero, dipendenti totali) solo se \
presenti nel resoconto, altrimenti null.
- Non aggiungere nulla che non sia nel resoconto.

<resoconto>
{resoconto}
</resoconto>"""


def estrai_dati(client: anthropic.Anthropic, azienda: str, resoconto: str) -> ProfiloAzienda:
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCHEMA_ESTRAZIONE},
        },
        messages=[{"role": "user", "content": PROMPT_ESTRAZIONE.format(azienda=azienda, resoconto=resoconto)}],
    ) as stream:
        response = stream.get_final_message()

    verifica_rifiuto(response, "estrazione")
    if response.stop_reason == "max_tokens":
        sys.exit("[MARCO] Output di estrazione troncato (max_tokens).")
    try:
        return ProfiloAzienda.model_validate(json.loads(testo_da_risposta(response)))
    except (json.JSONDecodeError, ValidationError) as e:
        sys.exit(f"[MARCO] JSON di estrazione non valido: {e}")


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
def stampa_report(profilo: ProfiloAzienda, fonti: list[str], fonti_ok: dict[str, bool], esito: Optional[dict]) -> None:
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
                stato = "" if fonti_ok.get(f, True) else "  ⚠ non raggiungibile"
                print(f"       ↳ {urlparse(f).netloc or f}{stato}")

    mancanti = [c for c in CATEGORIE[:-1] if not any(b.categoria == c for b in profilo.benefit)]
    if mancanti:
        print(f"\n  Nessuna informazione trovata per: {', '.join(mancanti)}")
    if profilo.note:
        print(f"\n  Note: {profilo.note}")

    print(f"\n  Pagine consultate dalla ricerca web: {len(fonti)}")
    if esito is None:
        print("\n  [dry-run] Nessuna scrittura su Supabase.")
    else:
        stato = "creata" if esito["azienda_creata"] else "già presente"
        print(f"\n  Supabase: azienda {stato} (id={esito['azienda_id']})")
        print(f"            {len(esito['inseriti'])} benefit inseriti, "
              f"{len(esito['gia_presenti'])} già presenti (saltati)")
    print(linea)


def salva_report_json(profilo: ProfiloAzienda, resoconto: str, fonti: list[str], esito: Optional[dict]) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "_" for c in profilo.nome.lower()).strip("_")
    path = REPORT_DIR / f"{slug}_{ts}.json"
    path.write_text(json.dumps({
        "generato_il": ts,
        "modello": MODEL,
        "profilo": profilo.model_dump(),
        "fonti_ricerca": fonti,
        "esito_supabase": esito,
        "resoconto_ricerca": resoconto,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="MARCO - ricerca benefit aziendali per Benevox")
    parser.add_argument("azienda", nargs="?", default="Ferrari", help="Nome dell'azienda (default: Ferrari)")
    parser.add_argument("--sede", default="Maranello", help="Sede per disambiguare la ricerca")
    parser.add_argument("--dry-run", action="store_true", help="Non scrivere su Supabase")
    args = parser.parse_args()

    env("ANTHROPIC_API_KEY")
    db = None
    if not args.dry_run:
        db = create_client(env("SUPABASE_URL"), env("SUPABASE_SECRET_KEY"))

    client = anthropic.Anthropic()

    print(f"[MARCO] 1/4 Ricerca web sui benefit di {args.azienda} ({args.sede})…")
    resoconto, fonti = ricerca_web(client, args.azienda, args.sede)
    print(f"[MARCO]     {len(fonti)} pagine consultate.")

    print("[MARCO] 2/4 Estrazione dati strutturati con Claude…")
    profilo = estrai_dati(client, args.azienda, resoconto)
    print(f"[MARCO]     {len(profilo.benefit)} benefit estratti.")

    fonti_citate = sorted({f for b in profilo.benefit for f in b.fonti})
    fonti_ok = {f: url_raggiungibile(f) for f in fonti_citate}

    esito = None
    if db is not None:
        print("[MARCO] 3/4 Salvataggio su Supabase…")
        esito = salva_su_supabase(db, profilo, args.azienda)
    else:
        print("[MARCO] 3/4 Salvataggio saltato (--dry-run).")

    print("[MARCO] 4/4 Report")
    stampa_report(profilo, fonti, fonti_ok, esito)
    print(f"[MARCO] Report completo salvato in {salva_report_json(profilo, resoconto, fonti, esito)}")


if __name__ == "__main__":
    try:
        main()
    except anthropic.AuthenticationError:
        sys.exit("[MARCO] ANTHROPIC_API_KEY non valida.")
    except anthropic.RateLimitError:
        sys.exit("[MARCO] Rate limit Anthropic raggiunto: riprova tra qualche minuto.")
    except anthropic.APIStatusError as e:
        sys.exit(f"[MARCO] Errore API Anthropic {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("[MARCO] Impossibile raggiungere l'API Anthropic (rete).")
