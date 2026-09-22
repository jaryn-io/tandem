# ForgeDesk, part 2: inherit a partial product, verify it, complete it

Public record `2026-08-30-forgedesk-2` · session of 2026-08-30 · 123 messages · duration 1h31 · total tokens 9252397

## Team

| Role | Model | Turns |
|---|---|---|
| Auditor | Claude Opus 4.6 (Thinking) | 3 |
| Challenger | Gemini 3.7 Flash | 3 |
| Orchestrator | Claude Haiku 4.5 | 30 |
| Planner | GPT-5.6 | 2 |
| Producer | Gemini 3.7 Flash | 14 |
| Reviewer | GLM 4.7 | 6 |
| Security | GPT-5.6 | 2 |

## Brief

ForgeDesk is a partially built web application for running a small makerspace. The existing code lives in the previous deliverable and must be used as the base of the work.

Examine every file directly and determine which features are genuinely working, incomplete, inconsistent, or only claimed in the documentation. The README is not proof of completeness: the actual behaviour of the code and the interface is the baseline. Keep and build on the valid existing work; do not start again from scratch without reason, and do not delete working data or features. Any database change must preserve or deterministically migrate the existing state.

Before building, propose a new complete plan based on your analysis of the current product and on the requirements below. The plan will be submitted for approval.

ForgeDesk must become a real, complete application usable through the browser, runnable locally on Linux with a single documented command. It must work without cloud services, external APIs or keys, and all important data must persist correctly across restarts.

It must support four roles: Administrator, with full control; Operator, responsible for daily operations but with no authority over general security settings; Member, limited to their own profile and reservations; Viewer, read-only.

Login, logout, sessions, resource ownership and authorisation must be enforced server-side on pages, forms and direct API requests. Documented demo accounts must exist for every role. Sessions must be protected, revoked when passwords or privileges change, and stored so that tokens extracted from the database cannot be used directly. Cookie-authenticated operations must be protected against CSRF, including the demo quick-login functions. Requests must have size limits, and errors returned to the browser must not reveal internal details. A configuration exposed beyond the local interface must not allow session cookies in clear text.

Every member must have contacts, membership status and qualifications tied to machine categories, with issue and expiry dates. A reservation or a use must be allowed only when all required qualifications are valid at the relevant moment, including the case of a qualification valid at booking time that expires before check-in.

Machines must have a category, capacity, operating hours, requirements, rates and a status: available, temporarily unavailable, under maintenance or retired. Maintenance and status changes must affect availability without deleting the reservation history.

The system must handle single and recurring reservations in the Europe/Rome time zone, including: capacity and overlaps; opening hours; maintenance periods; qualifications; modification and cancellation; waiting list; atomic promotion of the first still-eligible member; check-in, check-out, lateness and no-show; recurrences across daylight-saving changes; isolated modification of a single occurrence. Concurrent requests must not produce overbooking or inconsistent states.

There must be an inventory of consumables and spare parts based on an immutable ledger, supporting receipt, reservation, consumption, release and justified manual correction. Available quantities must always be derivable from the ledger and must never go negative, even under concurrent requests.

Machines must support hourly prices, minimum amounts and peak/off-peak bands. Money must be represented without floating-point errors. At checkout a charge must be computed from actual usage and the rules in force at that moment. A finalised charge must remain unchanged if rates change later; administrators can create explicit adjustments without rewriting the original.

Operators and Administrators must be able to manage maintenance work and incidents, with priority, assignment and progress. Incidents may include attachments with size and format limits, safely generated storage names, and protection against path traversal and executable content. A critical incident must be able to make a machine unavailable without deleting existing reservations.

The interface must be consistent, responsive and genuinely operational. It must include at least: an operations dashboard; a reservations calendar; members and qualifications; machines, maintenance and incidents; inventory and its ledger; charges and adjustments; a searchable audit history. Forms must keep the entered values when validation fails and explain conflicts and errors clearly.

CSV imports with preview and per-row validation must be available. A failed import must leave no partial changes. Useful CSV or JSON exports and a documented backup and restore procedure are required.

Every sensitive or business-relevant change must produce an append-only audit event with actor, timestamp, action, affected object and before/after values. A failure of the audit must not let the main change be committed without a record. Operations involving several objects must be atomic and always leave a valid state.

The final result must include complete code, a persistent database, migrations, representative demo data and correct operating documentation. No placeholder screens, simulated actions, hard-coded success responses or essential features described only as future work may remain.

*Translated from Italian; the original follows.*

<details><summary>Original (Italian)</summary>

ForgeDesk è un’applicazione web parzialmente realizzata per la gestione completa di un piccolo
  makerspace. Il codice esistente si trova in the project folder e
  deve essere usato come base del lavoro.

  Esamina direttamente tutti i file presenti e determina quali funzionalità siano realmente operative,
  incomplete, incoerenti o soltanto dichiarate nella documentazione. Il README non costituisce prova
  di completezza: il comportamento effettivo del codice e dell’interfaccia è la baseline. Conserva e
  integra il lavoro valido esistente; non ricominciare inutilmente da zero e non eliminare dati o
  funzionalità funzionanti. Eventuali modifiche al database devono preservare o migrare
  deterministicamente lo stato esistente.

  Prima della realizzazione, proponi un nuovo piano completo basato sulla tua analisi del prodotto
  attuale e sui requisiti seguenti. Il piano sarà sottoposto ad approvazione.

  ForgeDesk deve diventare un’applicazione reale, completa e utilizzabile tramite browser, eseguibile
  localmente su Linux con un solo comando documentato. Deve funzionare senza servizi cloud, API
  esterne o chiavi, e tutti i dati importanti devono persistere correttamente dopo il riavvio.

  Deve supportare quattro ruoli:

  - Administrator, con controllo completo;
  - Operator, responsabile delle operazioni quotidiane ma senza autorità sulle impostazioni di
    sicurezza generali;

  - Member, limitato al proprio profilo e alle proprie prenotazioni;
  - Viewer, con accesso esclusivamente in lettura.

  Login, logout, sessioni, proprietà delle risorse e autorizzazioni devono essere applicati dal server
  su pagine, form e richieste API dirette. Devono essere disponibili account dimostrativi documentati
  per ogni ruolo. Le sessioni devono essere protette, revocate quando cambiano password o privilegi e
  conservate in modo da non rendere utilizzabili direttamente eventuali token estratti dal database.
  Le operazioni effettuate tramite cookie devono essere protette da CSRF, comprese le funzioni
  dimostrative di accesso rapido. Le richieste devono avere limiti di dimensione e gli errori
  restituiti al browser non devono rivelare dettagli interni. Una configurazione esposta oltre
  l’interfaccia locale non deve consentire cookie di sessione trasmessi in chiaro.

  Ogni membro deve avere contatti, stato dell’iscrizione e qualifiche associate alle categorie di
  macchine, con data di emissione e scadenza. Una prenotazione o un utilizzo devono essere consentiti
  soltanto quando tutte le qualifiche necessarie sono valide nel momento pertinente, compreso il caso
  in cui una qualifica sia valida alla prenotazione ma scada prima del check-in.

  Le macchine devono avere categoria, capacità, orari operativi, requisiti, tariffe e stato:
  disponibile, temporaneamente non disponibile, in manutenzione oppure ritirata. Manutenzioni e cambi
  di stato devono influire sulla disponibilità senza cancellare lo storico delle prenotazioni.

  Il sistema deve gestire prenotazioni singole e ricorrenti nel fuso Europe/Rome, compresi:

  - capacità e sovrapposizioni;
  - orari di apertura;
  - periodi di manutenzione;
  - qualifiche;
  - modifica e cancellazione;
  - lista d’attesa;
  - promozione atomica del primo membro ancora idoneo;
  - check-in, check-out, ritardo e no-show;
  - ricorrenze attraverso i cambi di ora legale;
  - modifica isolata di una singola occorrenza.

  Richieste concorrenti non devono produrre overbooking o stati incoerenti.

  Deve essere presente un inventario di materiali consumabili e ricambi basato su un ledger
  immutabile. Deve supportare ricezione, prenotazione, consumo, rilascio e correzione manuale
  motivata. Le quantità disponibili devono essere sempre derivabili dal ledger e non devono mai
  diventare negative, nemmeno con richieste concorrenti.

  Le macchine devono supportare prezzi orari, importi minimi e fasce peak/off-peak. Il denaro deve
  essere rappresentato senza errori in virgola mobile. Al checkout deve essere calcolato un addebito
  basato sull’utilizzo reale e sulle regole applicabili in quel momento. L’addebito finalizzato deve
  rimanere invariato se le tariffe cambiano successivamente; gli amministratori possono creare
  rettifiche esplicite senza riscrivere l’originale.

  Operator e Administrator devono poter gestire interventi di manutenzione e incidenti, con priorità,
  assegnazione e avanzamento. Gli incidenti possono includere allegati con limiti di dimensione e
  formato, nomi di archiviazione generati in sicurezza e protezione da path traversal e contenuti
  eseguibili. Un incidente critico deve poter rendere indisponibile una macchina senza eliminare le
  prenotazioni esistenti.

  L’interfaccia deve essere coerente, responsive e realmente operativa. Deve comprendere almeno:

  - dashboard operativa;
  - calendario delle prenotazioni;
  - membri e qualifiche;
  - macchine, manutenzioni e incidenti;
  - inventario e relativo ledger;
  - addebiti e rettifiche;
  - cronologia audit ricercabile.

  I form devono conservare i valori inseriti quando la validazione fallisce e spiegare conflitti ed
  errori in modo comprensibile.

  Devono essere disponibili importazioni CSV con anteprima e validazione per riga. Un import fallito
  non deve lasciare modifiche parziali. Servono esportazioni CSV o JSON utili e una procedura
  documentata di backup e ripristino.

  Ogni modifica sensibile o rilevante per il business deve produrre un evento audit append-only
  contenente attore, timestamp, azione, oggetto interessato e valori precedenti e successivi. Il
  fallimento dell’audit non deve consentire alla modifica principale di essere confermata senza
  registrazione. Le operazioni che coinvolgono più oggetti devono essere atomiche e lasciare sempre
  uno stato valido.

  Il risultato finale deve includere codice completo, database persistente, migrazioni, dati
  dimostrativi rappresentativi e documentazione operativa corretta. Non devono rimanere schermate
  segnaposto, azioni simulate, risposte di successo hardcoded o funzionalità essenziali descritte
  soltanto come lavoro futuro.

</details>
