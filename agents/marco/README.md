# MARCO — ricerca benefit aziendali

Agente di Benevox che cerca sul web i benefit di un'azienda italiana, li struttura
con Claude e li salva su Supabase (tabelle `aziende` e `benefit`).

## Setup

```bash
cd agents/marco
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # poi inserisci ANTHROPIC_API_KEY e SUPABASE_SECRET_KEY
```

## Uso

```bash
python marco.py                          # Ferrari, Maranello
python marco.py "Ferrari" --dry-run      # solo ricerca + report, nessuna scrittura
python marco.py "Barilla" --sede Parma
```

## Come funziona

1. **Ricerca** — Claude (`claude-opus-5-5`) con il tool `web_search` esplora sito
   ufficiale, accordi integrativi, stampa e recensioni, e produce un resoconto con URL.
2. **Estrazione** — una seconda chiamata trasforma il resoconto in JSON validato
   (schema rigido) con categoria tra `smart_working`, `mensa`, `welfare`, `sanita`,
   `premi`, `sconti`, `altro` e un livello di affidabilità.
3. **Controllo fonti** — `requests` verifica che gli URL citati siano raggiungibili.
4. **Salvataggio** — l'azienda viene cercata per nome (case-insensitive): se esiste
   si completano solo i campi vuoti, altrimenti viene creata. I benefit già presenti
   con lo stesso nome vengono saltati; i nuovi sono inseriti con `verificato = false`.
5. **Report** — stampato a terminale e salvato in `reports/<azienda>_<timestamp>.json`
   (include resoconto e fonti, utile per la verifica manuale).
