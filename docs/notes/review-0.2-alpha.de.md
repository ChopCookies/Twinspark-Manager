> **Historical note, in German.** Review of the 0.2 alpha build (September 2026): what was broken then and how 0.2 fixed it. Commands and file names in it are from that version; see the README and CHANGELOG for the current state.

# Review der Alpha-Build → 0.2

Kurzfassung: Die Architektur des Agents (Controller / typisierter Agent / Gateway, unveränderliche Revisionen, UMA-Planner) war richtig gedacht. In der gelieferten Form hätte das Programm aber nicht funktioniert: Es startet nicht, und hätte es gestartet, hätte jede Aktivierung „HEALTHY“ gemeldet, ohne einen Container zu starten. Unten stehen alle gefundenen Fehler, danach der Stand von 0.2 und was noch offen ist.

## A. Kritische Fehler (Programm läuft nicht / tut nichts)

| # | Datei | Problem | Folge |
|---|---|---|---|
| A1 | `api/routes_profiles.py` | `list_profiles(controller: Controller)` ohne `Depends` | FastAPI bricht schon beim Import ab, die App startet nie |
| A2 | `controller/jobs.py` + `state_machine.py` | `ActivationStateMachine.run()` ist synchron, ruft aber einen `async`-Handler auf, und das in `asyncio.to_thread` | Die Coroutines werden nie ausgeführt; jede Stufe gilt als „ok“, **jede Aktivierung meldet HEALTHY, ohne dass etwas passiert** |
| A3 | `state_machine.py` | Fehler werden abgefangen und mit `break` verworfen | Selbst ein echter Fehler würde vom Controller als `COMPLETED` gespeichert |
| A4 | `controller.py` | `activate()` ist synchron und ruft `asyncio.create_task()` aus einem Threadpool-Handler auf | `RuntimeError: no running event loop` → Endpoint liefert 409 |
| A5 | `store.py` | SQLite ohne `check_same_thread=False` | `ProgrammingError`, sobald zwei Threads zugreifen (FastAPI-Threadpool) |
| A6 | gesamt | Kein Entry-Point: `tsm controller start` / `tsm agent start` aus der README gibt es nicht; nichts startet uvicorn | Kein Dienst lauffähig, keine systemd-Units |
| A7 | `gateway/gateway.py` | `backend` wird nirgends gesetzt (Controller übergibt ihn nie) | Das Gateway hätte dauerhaft 503 geliefert |
| A8 | `api/deps.py` | `management_api_key` wird nie angelegt | Alle geschützten Endpunkte liefern 503 |
| A9 | `agent/actions.py` | Alles Stubs: `container_start` liefert eine Fake-ID, `health` immer `True` | Kein echter Start möglich |
| A10 | `schemas/profile.py` | Es wird nirgends ein `vllm serve`-Befehl erzeugt; TP-Größe, Host/Port, Modellpfad und Worker-Rolle fehlen völlig | Selbst mit echtem Agent gäbe es nichts zu starten |

## B. Fachliche Fehler

- **`--gpu-memory-utilization` wurde nie gesetzt.** vLLM reserviert standardmäßig 90 % des *gesamten* Speichers. Auf dem UMA-Spark teilen sich OS, Docker und Manager diesen Speicher, das führt direkt zum OOM. Der Wert wird jetzt aus der Reserve berechnet (max. 0.88).
- Planner: `REPLICATED` wurde als gesharded gerechnet (halbe Gewichte), obwohl jede Node eine volle Kopie hat. Der KV-Cache wurde bei TP2/PP2 nicht geteilt. `num_params` steht nicht in `config.json`, dadurch wurden die Gewichte mit 0 GiB berechnet. `head_dim` aus der Config wurde ignoriert, und MLA-Modelle (DeepSeek-Stil) wurden falsch gerechnet.
- `/memory/estimate` hat bei unbekannten Modellen stillschweigend die Zahlen von „qwen3-coder“ verwendet. Jetzt gibt es in dem Fall eine 422 statt falscher Ergebnisse.
- Autopilot: `MAX_QUALITY` hat BF16 gesetzt, gerechnet wurde aber weiter mit der Original-Quantisierung. Der Vorschlag „eager“ wurde nie angewendet, und `best_that_fits` hat seine Argumente ignoriert. Der Status „VERIFIED“ für BF16/FP8 war hart kodiert.
- Manager-eigene Flags ließen sich über andere Schreibweisen umgehen (`--tensor_parallel_size`, `Port`). Der Filter in `effective_flags` hat nie gegriffen (`-` vs. `_`).
- Revision-IDs enthielten einen ISO-Zeitstempel (`:`, `+`) und waren damit nicht URL-tauglich. `duplicate()` hat die alten Revision-IDs und Profilnamen behalten.
- Resume in der State Machine war kaputt: `str`-Enums hashen über den Namen, nicht über den Wert.
- `_stop` hat `old_nodes=["A","B"]` hart kodiert, was zum `KeyError` führt, wenn B fehlt. Der Health-Check lief nur auf A. Beim Draining wurde der *neue* Alias gedrained statt der alten.
- Rollback: nur `create_task` ohne Referenz, ohne Neustart der vorherigen Version. Ein Fehler *vor* dem Stoppen hätte das alte Modell trotzdem im Drain-Zustand hängen lassen.
- Gateway: `/v1/completions` wurde an `/v1/chat/completions` weitergeleitet. Streaming wurde vollständig gepuffert (kein `stream=True`). Pro Request wurde ein zufälliger Backend-Key erzeugt. Der Client-Key war zufällig, wurde nie angezeigt und nicht zeitkonstant verglichen. Das `model`-Feld wurde nicht auf den Served-Name umgeschrieben (vLLM hätte `default` abgelehnt).
- `split` ließ sich speichern, war aber mit einem einzelnen Modell pro Profil nicht umsetzbar. Das wird jetzt explizit abgelehnt.
- Model Behaviour (Parser, Chat-Template, Sampling-Defaults) aus der ursprünglichen Spec fehlte komplett.

## C. Sicherheit

- Der Agent hatte **keine Authentifizierung** (`x-node-id` ist frei setzbar). Jeder im Netz hätte Container starten und stoppen können. Jetzt gilt ein Bearer-Token aus dem Vault mit zeitkonstantem Vergleich.
- Der Agent war nur an 127.0.0.1 gebunden, damit wäre Node B vom Controller aus nicht erreichbar gewesen. Jetzt ist die Bindung konfigurierbar; empfohlen wird die QSFP-IP.
- Profil-Endpunkte waren ohne Auth erreichbar. CSRF-Guard und Rate-Limit waren definiert, aber nie eingehängt. Das Rate-Limit arbeitete zudem mit einem 1-Sekunden-Fenster statt pro Minute.
- privd: Der Root-Socket wurde mit Default-umask angelegt, sodass jeder lokale User Root-Operationen hätte auslösen können. Jetzt ist er 0660 root:twinspark und läuft nur als root. Außerdem konnte ein einzelnes `read(65536)` Anfragen abschneiden.
- Vault: Die Schlüsseldatei war kurz world-readable, bevor `chmod` griff, und das Verzeichnis war nicht 0700. `_ensure_master_key` hatte zudem eine Typverwechslung.
- Der Agent akzeptierte beliebige Mounts. Jetzt sind nur Mounts unter `hf_cache_dir` und `compile_cache_dir` erlaubt, Images nur mit Digest, Containernamen nur mit `tsm-*` und Owner-Label.

## D. Was 0.2 neu hat

- `controller/launch.py`: Profil → exakte `docker run … vllm serve …`-Specs pro Node (single, replicated, tp2, pp2, tp-ep; native oder Ray). Einsehbar mit `tsm plan`, Secrets geschwärzt.
- Agent mit zwei Runtimes: `docker` (argv, nie Shell) und **`dry-run`**. Dry-run protokolliert alles und simuliert gesunde Container. Damit lässt sich der Manager neben deinem laufenden vLLM installieren, ohne es anzufassen.
- **Koexistenz:** Stoppen erfolgt nur per Label `org.twinspark.owned=true`, mit einer zweiten Label-Prüfung direkt vor dem `docker stop`. Die Preflight-Prüfung erkennt, wenn ein fremder Prozess den vLLM-Port belegt, und bricht *vor* jeder Änderung ab.
- Echte Pipeline: Preflight → Image-Pull → Download (HF, Hintergrund-Task) → rsync A→B über QSFP → Drain (wartet auf laufende Requests) → Stop → Reclaim (wartet, bis der UMA-Speicher wieder frei ist) → Start (in Wellen) → Health mit Phasenanzeige aus dem Log → Smoke-Test → Routing. Fortschritt wird nach jeder Stufe persistiert.
- Rollback: Bei einem Fehler vor dem Stop läuft das alte Modell einfach weiter. Bei einem Fehler danach werden die Teil-Container entfernt und die vorherige Known-Good-Version automatisch neu gestartet.
- Controller-Neustart übernimmt laufende Container, statt sie neu zu starten. Nach einem Reboot startet die letzte aktive Version automatisch. Unterbrochene Jobs werden als solche markiert.
- `tsm init` (Secrets), `tsm serve`, systemd-Units mit `MemoryMax` und `OOMScoreAdjust`, Beispiel-Configs und ein Beispielprofil.
- 35 Tests (pytest) plus ein Prozess-Smoke-Test mit echten uvicorn-Servern, zwei Dry-Run-Agents und der CLI.

## E. Offen / bewusst nicht gemacht

- **Web-GUI fehlt komplett.** `twinspark/web/dist` existiert im Alpha nicht; die API ist fertig.
- Cookbook-Import, `split`-Profile, Headless-Apply (privd führt noch kein `systemctl` aus), NCCL-/QSFP-Test-Actions.
- Auflösung HF-Branch → Commit-SHA und Image-Tag → Digest. Beides muss aktuell explizit angegeben werden.
- eugr `launch-cluster.sh` und dessen Mods werden nicht aufgerufen. Der Manager startet die Container selbst (mit eugr-Image möglich); NCCL-Variablen werden aus der Config gesetzt. Ob eugr-spezifische Patches/Mods gebraucht werden, muss Phase 0 zeigen.
- mTLS zwischen den Nodes: aktuell Bearer-Token über den direkten QSFP-Link.
- Download-Fortschritt in Prozent, vLLM-`/metrics`, Kalibrierung aus realen Messungen.
- Die Flags `--nnodes/--node-rank/--master-addr/--headless` (Multi-Node ohne Ray), `--default-chat-template-kwargs`, `--attention-backend` und `VLLM_API_KEY` müssen gegen das gepinnte Image geprüft werden.
- Wenn ein fremdes vLLM Speicher belegt, sieht der Planner das nur indirekt (MemAvailable). Deshalb beim ersten echten Test das alte vLLM vorher stoppen.
