# MARCO — ricerca benefit aziendali

Agente di Benevox che cerca sul web i benefit di un'azienda italiana, li struttura
con Groq (`llama-3.3-70b-versatile`) e li salva su Supabase (tabelle `aziende` e `benefit`).

## Setup

```bash
cd agents/marco
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # poi inserisci GROQ_API_KEY e SUPABASE_SECRET_KEY
```

## Uso

```bash
python marco.py                          # Ferrari, Maranello
python marco.py "Ferrari" --dry-run      # solo ricerca + report, nessuna scrittura
python marco.py "Barilla" --sede Parma
python marco.py "Ferrari" --url https://www.esempio.it/articolo   # pagine extra da leggere
```

## Come funziona

1. **Ricerca**: `requests` interroga DuckDuckGo (versione HTML, senza chiave) con
   più query (benefit, welfare, premio di competitività, smart working, mensa e
   sanità, sconti, recensioni). Scarica le pagine trovate e tiene solo le righe che
   parlano di benefit (fino a 10 pagine e circa 20.000 caratteri, per restare nei
   limiti del piano gratuito Groq).
2. **Estrazione**: Groq (`llama-3.3-70b-versatile`, modalità JSON) trasforma gli
   estratti in dati strutturati. Ogni benefit ha una categoria tra `smart_working`,
   `mensa`, `welfare`, `sanita`, `premi`, `sconti`, `altro`, un livello di
   affidabilità e le pagine di provenienza. Il risultato è validato con Pydantic;
   se il JSON non è valido, il modello riceve l'errore e ha un secondo tentativo.
3. **Salvataggio**: l'azienda viene cercata per nome (senza distinguere maiuscole e
   minuscole). Se esiste, si completano solo i campi vuoti, altrimenti viene creata.
   I benefit già presenti con lo stesso nome vengono saltati; i nuovi sono inseriti
   con `verificato = false`.
4. **Report**: stampato a terminale e salvato in `reports/<azienda>_<timestamp>.json`,
   con gli estratti delle pagine lette, utile per la verifica manuale.

Se DuckDuckGo limita le richieste (nessun risultato), riprova più tardi oppure passa
gli URL direttamente con `--url`.
