# Positive Memory · ForgeDesk, part 2: inherit a partial product, verify it, complete it

# Positive memory candidate

### 1. Gestione sicura del ciclo di vita WAL/SHM nel backup e restore di SQLite
Nelle applicazioni SQLite con journaling WAL (`PRAGMA journal_mode=WAL`), la semplice sostituzione del file principale `.db` (es. tramite `os.replace` o copia file) durante un restore a caldo o a riposo è rischiosa se non vengono gestiti i file ausiliari `-wal` e `-shm`:
- **Prima dello snapshot di sicurezza o backup**: eseguire `PRAGMA wal_checkpoint(TRUNCATE)` per sincronizzare e svuotare i frame pendenti nel database principale.
- **Dopo la sostituzione del file `.db`**: rimuovere esplicitamente i file `.db-wal` e `.db-shm` residui prima di riaprire nuove connessioni, per evitare che SQLite legga frame non allineati appartenenti al database precedente.

### 2. Immutabilità a livello di motore dati tramite trigger SQLite
Quando i requisiti di business impongono ledger contabili, storici di magazzino o registri di audit append-only, la sola validazione a livello applicativo non garantisce la non ripudiabilità né la protezione da update accidentali:
- Configurare trigger SQLite `BEFORE DELETE` e `BEFORE UPDATE` con `SELECT RAISE(ABORT, '...')` su tabelle di ledger (`inventory_ledger`, `audit_log`, `charge_adjustments`).
- Per record con snapshot iniziale immutabile ma stato operativo modificabile (es. `usage_charges` con tariffa calcolata e successivo aggiustamento amministrativo), implementare trigger condizionali che bloccano la modifica delle colonne di snapshot originale (`rate_breakdown_json`, `base_charge_cents`, `start_time`, `end_time`) consentendo l'aggiornamento solo dei campi di stato/rettifica.

### 3. Timestamp server-authoritative per eventi a impatto tariffario
Nei flussi operativi che determinano addebiti o penalità (es. check-in e check-out di prenotazioni e macchinari):
- Il server deve sempre generare il timestamp corrente (`server_now`) come valore predefinito e autorevole, ignorando input temporali inviati da utenti standard.
- Consentire l'inserimento manuale di timestamp alternativi esclusivamente ad account con privilegi elevati (operatore/amministratore), vincolando comunque la richiesta a controlli di coerenza stretti (limite su date future oltre il margine di clock drift e validazione che `checkout >= checkin`).
