# Samostojna storitev za anonimizacijo

Preprost slovenski GUI za PDF-je, ki uporablja obstoječa `agent-api` in `workspace-api`. Projekt je samostojen: ne uvaža kode iz `agent_v6_deep`, nima dostopa do njegove baze ali diskov in ne zaganja lastnega modela. Podprti so PDF-ji do 40 MB; omejitve agenta (trenutno največ 20 strani) veljajo še naprej.

## Zagon z Dockerjem

```bash
cd anonymizer_service
cp .env.example .env  # le pri prvi namestitvi
# Nastavite naslova obeh API-jev v .env.
docker compose up -d --build
```

GUI: **http://localhost:8085**. Obstoječi agent mora teči ločeno. Lokalna konfiguracija doseže njegova objavljena porta 8081 in 8090 prek `host.docker.internal`. V produkciji nastavite dejanska notranja HTTPS naslova storitev; ne uporabljajte `localhost` za oddaljene vsebnike.

Za lokalni Python zagon:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
AGENT_API_URL=http://localhost:8081 WORKSPACE_API_URL=http://localhost:8090 DATA_DIR=./data uvicorn app:app --host 127.0.0.1 --port 8085 --no-access-log
```

Okoljske spremenljivke v `.env` se v lokalnem Python zagonu ne naložijo samodejno; Docker Compose jih naloži prek `env_file`.

## Tok API-jev

1. Prijava prek `POST /v1/auth/login`; veljavnost in identiteta prek `GET /v1/auth/me`. GUI podpira tudi že pridobljen `guid` (npr. za zunanji AuthProvider).
2. Storitev hrani `guid` samo na strežniku, lokalno v SQLite. Brskalnik dobi naključno HttpOnly/SameSite cookie. Pri vsakem zasebnem klicu agentu storitev doda query parameter `guid`, kot zahteva njegov API.
3. Datoteka gre v `POST workspace-api/v1/files` z ločeno mapo za vsako opravilo.
4. `POST agent-api/v1/threads` ustvari thread, nato `POST /v1/threads/{thread_id}/runs` zažene anonimizacijo v ozadju. Zahtevani JSON odgovor vsebuje `status`, `output_path`, `summary`.
5. GUI na 2,5 sekunde preverja stanje. Backend uporablja `GET /v1/runs/{run_id}` in ob zaključku `GET /v1/threads/{thread_id}/messages`. Storitev razbere JSON (tudi besedilne bloke in Markdown ograje) ter preveri dosegljivost PDF-ja prek workspace-api. Sprejme vnaprej določeno pot ali drugo PDF-pot pod `/exports`, katere ime vsebuje UUID istega opravila. Tuje oziroma nedoločljive poti zavrne.
6. Prenos teče prek `GET workspace-api/v1/files/{path}` in tega webservisa; `guid` se ne razkrije brskalniku.

Vsak račun vidi samo svoja opravila in prenose. Zgodovina ter povezave do agentovih opravil so v trajnem Docker volumnu `service_data`. Ob obnovitvi strani se status nadaljuje; ob ponovnem zagonu tega webservisa se spremljanje že potrjenih agentovih opravil nadaljuje. Ponovni zagon agent-api prekine njegove opravke v pomnilniku: storitev tedaj pokaže napako. Če potrditev zagona ne prispe, storitev ohrani stanje »preverjam povezavo« ter prek aktivnega opravila in shranjenih sporočil istega threada obnovi spremljanje ali rezultat. Zahteve za anonimizacijo ne pošlje ponovno. Samo nalaganja, prekinjena pred ustvarjanjem threada, se označijo kot neuspešna. Storitev uporablja en uvicorn worker.

Če odgovor ne potrjuje uspešne anonimizacije, PDF na poti tega opravila pa je dosegljiv, je rezultat `review_required`: GUI izrecno opozori, da anonimizacija ni potrjena, in omogoči prenos samo za ročni pregled. Tak prenos ima ime `_nepotrjeno_pregled.pdf`. Preverjanje `%PDF-` glave potrdi dosegljivost PDF vsebine, ne popolnosti datoteke ali uspešnosti prekrivanja. Manjkajoč/neustrezen PDF ostane napaka. Po posodobitvi se stare napake »Agent ne potrjuje izdelave anonimiziranega dokumenta.« ponovno preverijo brez novega zagona anonimizacije.

Rezultat je vedno kopija za človeški pregled, ne potrdilo popolne anonimizacije. Izvirni dokumenti in rezultati ostanejo v uporabnikovem workspaceu pri agentu. Odjava izbriše samo relacijo te storitve, zato ne prekine drugih agentovih odjemalcev.

## Produkcija

- Objavite za HTTPS reverse proxyjem; nastavite `COOKIE_SECURE=true` in `PUBLIC_ORIGIN=https://dejanska-domena`.
- Ohranite trajen `/data` volumen. SQLite vsebuje prijavne tokene: omejite dostop do volumna in varnostnih kopij. Datoteka baze ima pravice 0600.
- API-ja naj bosta omrežno dosegljiva samo strežniku storitve. CORS in delitev cookie domene z agentom nista potrebna.
- Reverse proxy mora omogočiti nalaganje do nastavljene velikosti. Dolg HTTP klic za celotno anonimizacijo ni potreben; spremljanje uporablja kratke zahteve.
- Lokalni `GET /health` preverja delovanje te storitve, ne povezave z agentom.

## Preverjanje

```bash
python -m unittest discover -s tests -v
```

Integracijski testi uporabljajo simulirana API-ja in preverijo prijavo, tok datoteka → thread → run → rezultat, prenos, ločitev uporabnikov, izvor zahtev ter zavračanje napačnih datotek. Ne kličejo modela.


## Diagnostika rezultata in healthchecka

Logi vsebujejo `result_received`, `artifact_probe`, `result_needs_review`, `result_rejected` ter `upstream_transport_error`/`upstream_http_error`. Navedejo ID opravila, razlog in vrsto napake oziroma status, ne prijavnih GUID, odgovorov agenta ali vsebine dokumentov. To loči napačno obliko odgovora, napačno pot, manjkajoč PDF in omrežno napako.

`unhealthy` izvira iz ločenega healthchecka lokalnega `/health`. Sam po sebi ne pomeni, da je agentova anonimizacija odpovedala. Compose uporablja interval 20 s, timeout 20 s in zagonsko obdobje 45 s; s tem počasen zagon Python preverjanja ne odpove že po 5 s. Dejanski razlog neuspelega preverjanja preverite v healthcheck zgodovini Dockerja/Podmana. Pri namestitvi ohranite svoje porte, naslove API-jev in omrežne nastavitve.

## Nastavitve iz ARM

Storitev ob zagonu najprej uporabi priloženo knjižnico ArmUtils. V `.env` nastavite
`ARM_APP_NAME` na pravo ime aplikacije v ARM (privzeto `anonymizer_service`) in
`ARM_SETTINGS_URL` na URL vašega okolja. Klic pošlje `sfObjekt=ARM_APP_NAME`;
`SF_NASTAVITEV` je ime obstoječe okoljske nastavitve, `SF_NASTAVITEV_VREDNOST` pa njena vrednost.
Na primer, ARM ključ `AGENT_API_URL` nadomesti istoimensko vrednost iz okolja.
V v6 veljajo enaka imena tudi za LLM tokene, podatkovno bazo, prijavo in druge aplikacijske nastavitve.

ARM vrednosti imajo prednost pred okoljem. Ključi, ki jih ARM ne vrne, obdržijo
okoljsko vrednost oziroma obstoječo privzeto vrednost aplikacije. Izrecno prazna
ARM vrednost ostane prazna. Če klic ne uspe, vrne prazen seznam ali neveljaven
seznam nastavitev, se uporabi okolje brez delnega uveljavljanja odgovora ARM.
Vsebinsko neveljavne aplikacijske nastavitve (npr. neveljavna meja tokenov)
še vedno zavrne obstoječa validacija; povezave z ARM to ne prikrijejo.

Povezava z ARM se nastavi izključno prek okolja: `ARM_APP_NAME`,
`ARM_SETTINGS_URL`, `ARM_TLS_VERIFY`, `ARM_CA_FILE`, `ARM_CA_PATH`,
`ARM_CLIENT_CERT_FILE`, `ARM_CLIENT_KEY_FILE`, `ARM_CLIENT_KEY_PASSWORD`.
Privzeto je preverjanje TLS vključeno. Za interno CA nastavite `ARM_CA_FILE`,
za mTLS pa še datoteki certifikata in zasebnega ključa; datoteke morajo biti
nameščene v vsebniku in poti nastavljene glede nanj. Bootstrap nastavitve morajo
biti dostopne pred ARM klicem in jih ni mogoče pridobiti iz ARM za isti prvi klic.
Privzeti URL knjižnice kaže na testni ARM; za produkcijo ga izrecno spremenite.

ARM se prebere enkrat na proces (rok HTTP klica v knjižnici je 10 sekund).
Za ponovno pridobitev, tudi po izpadu ARM, znova zaženite proces. Streamlit ob
vsaki interakciji ne kliče ARM ponovno. Pri spremembi `.env` ponovno ustvarite
vsebnik; `.env` pri neposrednem Python zagonu sami naložite v okolje.
Nastavitve Dockerja/Podmana (objava portov, priklopi datotek, build, uporabnik
vsebnika) ostajajo stvar konfiguracije namestitve in jih aplikacijski ARM ne spreminja.

Knjižnica in dodatek za nalaganje okolja sta vključena v `vendor/armutils`, da je
projekt samostojen. Odvisnosti namestite iz korena tega projekta z
`pip install -r requirements.txt`;
Dockerfile knjižnico namesti samodejno. Ob posodobitvi izvornega `armutils-main`
osvežite tudi vključene kopije. Po teh spremembah je potreben nov build slike.
