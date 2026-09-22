# Plan · ForgeDesk, part 2: inherit a partial product, verify it, complete it

## Revision 1 (message 005) — rejected by the human

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | producer | S01 |
| S03 | producer | S01 |
| S04 | producer | S02, S03 |
| S05 | security | S02, S03, S04 |
| S06 | reviewer | S02, S03, S04 |
| S07 | challenger | S05, S06 |
| S08 | producer | S05, S06, S07 |
| S09 | auditor | S08 |

**Human, on this revision:**

> No. The plan is logically sound, but it ignores the current state. The Producer steps are too large: they bundle too many things, so they must be split with intermediate reviews. And to make the plan you have to take stock of the real situation by reading it. Good use of the Challenger, more than once if needed. Otherwise the plan must be revised in the light of what I said.
> 
> *Translated from Italian; the original follows.*

## Revision 2 (message 010) — approved

Plan revision 2

Procedo con una revisione del piano ancorata al codice ForgeDesk realmente presente, mantenendo gli step già esistenti quando possibile e introducendo cicli intermedi di review/challenge. Verifico prima struttura e stato effettivo del deliverable dichiarato.
L’ispezione della base reale mostra un’applicazione Python/SQLite già articolata, con autenticazione, audit, membri, macchine, manutenzioni e un dominio prenotazioni esteso. Sono però presenti lacune concrete: `inventory`, addebiti, import/export e backup non risultano come moduli applicativi dedicati; la UI e le route devono essere verificate end-to-end; esistono implementazioni incomplete (`pass`) nella ricorrenza e in parti della disponibilità; incidenti, allegati, sicurezza trasversale e persistenza devono essere provati contro il comportamento effettivo. Il database esistente e le migrazioni dovranno essere trattati come stato da preservare.

Il nuovo piano mantiene una sequenza incrementale, con producer più piccoli e controlli indipendenti dopo ciascun gruppo funzionale.

S01 — Producer: eseguire l’inventario tecnico della base ForgeDesk, includendo struttura, entrypoint `run.sh`, schema SQLite, migrazioni, seed, route HTML/API, moduli presenti, test disponibili e funzionalità dichiarate ma non dimostrate. Produrre una baseline dei gap e dei vincoli di compatibilità senza modificare inutilmente il prodotto.

S02 — Producer: consolidare avvio locale, configurazione, connessione al database, migrazioni idempotenti, seed dimostrativi e persistenza dopo riavvio, preservando e migrando deterministicamente il database esistente.

S03 — Reviewer: verificare la baseline tecnica e le fondazioni di S02, controllando che il comando locale sia ripetibile, che lo schema sia coerente e che nessun dato esistente venga perso.

S04 — Producer: completare autenticazione e autorizzazione server-side per Administrator, Operator, Member e Viewer, comprese proprietà delle risorse, account dimostrativi, logout, revoca delle sessioni dopo cambio password o privilegi e protezione dei token persistiti.

S05 — Security: valutare S04 su sessioni, password, cookie, CSRF inclusi i quick-login, autorizzazioni dirette su pagine/form/API, limiti delle richieste, errori esposti e configurazioni non-localhost.

S06 — Producer: completare membri, contatti, stato d’iscrizione e qualifiche con emissione, scadenza e associazione alle categorie macchina; integrare i controlli di idoneità nei flussi che ne dipendono.

S07 — Reviewer: verificare ruoli, ownership, qualifiche e scadenze sia tramite interfaccia sia tramite richieste API dirette, distinguendo comportamenti realmente operativi da documentazione o schermate non collegate.

S08 — Producer: completare macchine, categorie, capacità, orari operativi, requisiti, tariffe, stati, manutenzioni e incidenti con priorità, assegnazione, avanzamento, allegati validati e impatto corretto sulla disponibilità senza cancellare lo storico.

S09 — Challenger: analizzare indipendentemente transizioni macchina/incidente/manutenzione, conflitti con prenotazioni esistenti, contenuti allegati, path traversal, nomi di archiviazione e casi limite di stato.

S10 — Producer: completare prenotazioni singole e disponibilità, includendo capacità, sovrapposizioni, orari, qualifiche, manutenzioni, modifica, cancellazione e transazioni concorrenti senza overbooking.

S11 — Reviewer: verificare il ciclo delle prenotazioni singole tramite browser e API, inclusi conflitti comprensibili, conservazione dei valori dei form dopo errore e persistenza dopo riavvio.

S12 — Producer: completare ricorrenze in `Europe/Rome`, cambi DST, modifica isolata di un’occorrenza, check-in, check-out, ritardi, no-show, lista d’attesa e promozione atomica del primo membro ancora idoneo.

S13 — Challenger: stressare le premesse del calendario e della lista d’attesa su DST, qualifiche scadute prima del check-in, concorrenza, cancellazioni, ricorrenze e promozioni non idonee.

S14 — Producer: implementare inventario e ledger append-only per ricezione, prenotazione, consumo, rilascio e correzioni motivate, con quantità derivabili dal ledger e protezione da quantità negative concorrenti.

S15 — Producer: implementare prezzi, fasce peak/off-peak, importi minimi, calcolo decimale al checkout, snapshot degli addebiti finalizzati e rettifiche esplicite senza alterare l’originale.

S16 — Security: verificare concorrenza, atomicità transazionale, integrità del ledger, immutabilità degli addebiti finalizzati, autorizzazioni sulle rettifiche e fallimento coordinato dell’audit.

S17 — Producer: completare dashboard, calendario e interfacce responsive per membri, qualifiche, macchine, manutenzioni, incidenti, inventario, addebiti e audit; rimuovere placeholder, azioni simulate e successi hardcoded.

S18 — Reviewer: eseguire la verifica funzionale end-to-end dell’interfaccia e delle API, inclusa coerenza tra dati mostrati e dati persistiti, gestione degli errori e accesso per ciascun ruolo.

S19 — Producer: completare importazioni CSV con anteprima e validazione per riga, rollback completo degli import falliti, esportazioni CSV/JSON, backup/ripristino, audit ricercabile, seed rappresentativi e documentazione operativa aggiornata.

S20 — Challenger: esaminare i percorsi di import/export, backup/ripristino, audit, errori parziali, compatibilità dei dati migrati e ogni funzionalità ancora soltanto dichiarata.

S21 — Producer: applicare esclusivamente le correzioni autorizzate dai finding di Reviewer, Security e Challenger, aggiornando codice, migrazioni, seed e documentazione e classificando esplicitamente ogni limitazione residua.

S22 — Auditor: verificare la catena completa requisito → implementazione → evidenza, l’eseguibilità locale senza servizi esterni, la persistenza, la sicurezza, la coerenza UI/API, l’assenza di placeholder e la correttezza della documentazione. Questo è il gate finale indipendente.

L’ultimo step è Auditor.

| Step | Owner | Depends on |
|---|---|---|
| S01 | producer | — |
| S02 | producer | S01 |
| S03 | reviewer | S02 |
| S04 | producer | S03 |
| S05 | security | S04 |
| S06 | producer | S05 |
| S07 | reviewer | S06 |
| S08 | producer | S07 |
| S09 | challenger | S08 |
| S10 | producer | S09 |
| S11 | reviewer | S10 |
| S12 | producer | S11 |
| S13 | challenger | S12 |
| S14 | producer | S13 |
| S15 | producer | S14 |
| S16 | security | S15 |
| S17 | producer | S16 |
| S18 | reviewer | S17 |
| S19 | producer | S18 |
| S20 | challenger | S19 |
| S21 | producer | S20 |
| S22 | auditor | S21 |
