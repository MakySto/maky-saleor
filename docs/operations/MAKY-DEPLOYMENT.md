# MAKY.STORE — prevádzka a aktualizácie Saleoru

**Referenčný stav: 2026-09-05**, po upgrade Core `3.22.50 → 3.22.68 → 3.23.31`
a Dashboard `3.22.2 → 3.23.32`.

Tento dokument je prevádzkový záznam a návod. Hodnoty overené priamo na stroji sú
označené **[overené]**. Hodnoty prevzaté z protokolov bez opätovnej kontroly sú
**[z protokolu]**. Neznáme veci sú **NOT_VERIFIED** — nenahrádzaj ich odhadom.

Pri ďalšom zásahu tento dokument **aktualizuj**, neprepisuj históriu.

---

## 0. Orientácia za 60 sekúnd

Sú to **tri samostatné nasadenia**. Nezamieňaj ich.

| | Core | Dashboard | Storefront |
|---|---|---|---|
| Čo to je | Python GraphQL API | statická admin SPA | Next.js web pre zákazníka |
| Kde beží | VPS `/opt/saleor`, systemd | S3 + CloudFront | **iný stroj**, `/opt/storefront` |
| Verejne | `api.maky.store` | `dashboard.maky.store` | `maky.store` |
| Ako sa aktualizuje | kód + venv + DB migrácie | build → upload do S3 prefixu | vlastný deploy storefront vlákna |
| Verzia dnes | **3.23.31** | **3.23.32** | commit `3089012` pripravený, **nenasadený** |

Rýchle overenie **Core a Dashboardu** (storefront nie — ten má vlastný stroj a vlákno, viď §5):

```bash
sudo -u saleor grep -m1 __version__ /opt/saleor/saleor/__init__.py   # Core verzia
systemctl is-active saleor saleor-celery saleor-celerybeat           # Core služby
curl -s https://dashboard.maky.store/ | grep -oE 'index-[A-Za-z0-9_-]+\.js'  # Dashboard build hash
```

Verziu Dashboardu z bežiaceho buildu (nie je nikde vypísaná, treba ju vytiahnuť
z minifikovaného bundlu — `<HASH>` doplň z predchádzajúceho príkazu):

```bash
curl -s https://dashboard.maky.store/index-<HASH>.js | grep -oE 'const \w+="3\.[0-9.]+"' | head -1
```

**Nepoužívame Vercel, Saleor Cloud ani Docker.** Neodvodzuj hosting z upstream
ukážok ani z reklamných odkazov v Dashboarde.

---

## 1. Core — presné identity

| Položka | Hodnota | |
|---|---|---|
| Verejné API | `https://api.maky.store/graphql/` | |
| SSH alias (VS Code) | `maky-saleor` | |
| Hostname | `ip-10-0-1-115` | [overené] |
| Produkčný adresár | `/opt/saleor` | [overené] |
| Vetva | `maky-production-patches-v3.23.31` | [overené] |
| **Commit kódu** | `91f50fc4b99eb199471abb10116ba5324c376342` | [overené] |
| Verzia | `3.23.31` | [overené] |

> Vetva môže byť pred `91f50fc4b9` o commity, ktoré menia **iba dokumentáciu**
> (napr. tento súbor). Kódový stav nasadenej 3.23.31 určuje `91f50fc4b9`.
> Pri porovnávaní použi `git diff 91f50fc4b9..HEAD --stat` — ak sú tam len `docs/`
> a `AGENTS.md`, nasadený kód sa nezmenil a netreba reštart.
| Aktívny venv | `/opt/saleor-venvs/saleor-GPHclGPK-py3.12` | [overené] |
| Predchádzajúci venv (3.22.68) | `/opt/saleor-venvs/saleor-s_49aIhu-py3.12` | [overené] |
| Pôvodný venv (3.22.50) | `/home/saleor/.cache/pypoetry/virtualenvs/saleor-c8cPNvN6-py3.12`, 531 MB | [overené] |
| Databáza | AWS RDS PostgreSQL **18.3**, `eu-central-1` | [overené] |
| Runtime | Python 3.12.3, Django 5.2.17, orjson 3.12.0 | [overené] |
| Dependency nástroj | **Poetry** + vlastný `poetry.lock` (nie upstream `uv.lock`) | |
| HW | 2 vCPU, 7 GB RAM, **swap 0 MB**, ~8 GB voľného disku | [overené] |

Služby (všetky musia bežať na **rovnakom** kóde aj venv):

```
saleor.service              gunicorn + uvicorn worker, 127.0.0.1:8000, --workers 3
saleor-celery.service       celery worker --concurrency=2
saleor-celerybeat.service   celery beat, DatabaseScheduler
```

Aktívne systemd drop-iny [overené] — venv sa prepína **cez ne**, nie editáciou unit súborov:

```
/etc/systemd/system/saleor.service.d/venv.conf
/etc/systemd/system/saleor-celery.service.d/venv.conf
/etc/systemd/system/saleor-celerybeat.service.d/venv.conf
```

### 1.1 ⚠️ Git remotes — najčastejší omyl

**`/opt/saleor` NEMÁ remote na `MakySto/maky-saleor`.** [overené]

```
/opt/saleor          origin → https://github.com/saleor/saleor.git   (upstream Saleor!)
```

MAKY repo je remote `backup` v **pracovnom** repozitári:

```
/home/ubuntu/src/maky-saleor    backup   → git@github.com:MakySto/maky-saleor.git
                                upstream → https://github.com/saleor/saleor.git
```

Dôsledok: `git -C /opt/saleor fetch origin maky-production-patches-...` **zlyhá**.
Kandidátne commity sa do `/opt/saleor` prenášajú **git bundle**, viď §3.4.

Pracovný repozitár a jeho worktrees [overené]:

```
/home/ubuntu/src/maky-saleor                  maky-production-patches-v3.22.50
/home/ubuntu/src/saleor-candidate-3.22.68     maky-production-patches-v3.22.68
/home/ubuntu/src/saleor-candidate-3.23.31     maky-production-patches-v3.23.31
```

### 1.2 🔴 Otvorené riziko: patche nie sú na GitHube

**Ani `maky-production-patches-v3.22.68`, ani `...-v3.23.31` nie sú pushnuté na
`MakySto/maky-saleor`.** [overené — `git ls-remote --heads backup`]

Presný problém: patchsety zatiaľ **nemajú potvrdenú samostatnú kópiu mimo tohto VPS**.
Serverové zálohy podľa vlastníka bežia, ale tento dokument ich obnoviteľnosť neoveruje
(rola nemá `rds:DescribeDBInstances`, viď §4.2), takže sa na ne pri obnove patchov
nespoliehaj. Nie je to blocker prevádzky, ale je to jednoduchá vec na dokončenie:

```bash
git -C /home/ubuntu/src/saleor-candidate-3.23.31 push backup maky-production-patches-v3.23.31
git -C /home/ubuntu/src/saleor-candidate-3.22.68 push backup maky-production-patches-v3.22.68
```

---

## 2. Dashboard — presné identity

| Položka | Hodnota |
|---|---|
| Verejná URL | `https://dashboard.maky.store/` |
| Verzia | **3.23.32** (upstream tag, SHA `0511b510c39152f959c0431787f47c5480ab689e`) |
| S3 bucket | `maky-store-static-936049945896` |
| Prefix | `dashboard/` |
| **Cieľ uploadu** | `s3://maky-store-static-936049945896/dashboard/` |
| CloudFront ID | `E5Y69895CVJ2P` |
| CloudFront doména | `d2lz8c9v8srwkm.cloudfront.net` |
| Origin ID | `maky-dashboard-s3` |
| **OriginPath** | `/dashboard` |
| IAM rola na upload | `maky-saleor-ec2-role` (rola EC2, žiadne kľúče) |

### 2.1 Prefix `/dashboard` je interný

```
Verejne:   https://dashboard.maky.store/index-D5x7e-8F.js
V S3:      s3://maky-store-static-936049945896/dashboard/index-D5x7e-8F.js
```

CloudFront prefix dopĺňa sám cez `OriginPath`. Preto:
- `APP_MOUNT_URI` zostáva **prázdne**, `STATIC_URL` je `/`;
- v `index.html` **nesmie** byť ani jeden `/dashboard/` v asset cestách;
- do **rootu** bucketu sa nenahráva nič.

Kontrola pred uploadom:

```bash
grep -oE '(src|href)="[^"]+"' build/dashboard/index.html | grep -c '/dashboard/'   # musí byť 0
```

### 2.2 Build sa robí na Saleor VPS

Dashboard sa **zostavuje na tom istom VPS ako Core** a odtiaľ ide priamo do S3.
Miesto zostavenia ≠ miesto poskytovania. Netreba nič sťahovať do PC ani cez CloudShell.

| Položka | Cesta |
|---|---|
| Zdrojový build adresár | `/home/ubuntu/build-dashboard-3.23.32/src` [overené] |
| Výstup buildu | `<build>/build/dashboard/` |
| Archív | `/home/ubuntu/maky-dashboard-3.23.32.tar.gz`, 11 MB |
| SHA256 archívu | `050025763512778fbfb06d0ae3d968048a2a228a0f0b8f7d3dec6a61f6f8eb32` |
| Rozbalený deploy | `/home/ubuntu/deploy-dashboard-3.23.32/` |
| Záloha predchádzajúceho | `/home/ubuntu/dashboard-s3-backup-20260905T194403Z` (144 súborov, 56 MB) |

Toolchain použitý pri 3.23.32: Node `v24.15.0` (systémový, nemenený), pnpm `11.13.1`
cez Corepack shim v `~/.local/bin`, `pnpm install --frozen-lockfile`.
**Pin si over v `package.json` cieľového tagu** — toto je historický údaj, nie pravidlo.

Verejná konfigurácia (zachovať pri každom builde):

```dotenv
API_URL=https://api.maky.store/graphql/
APP_MOUNT_URI=
STATIC_URL=/
IS_CLOUD_INSTANCE=
LOCALE_CODE=
EXTENSIONS_API_URL=https://apps.saleor.io/api/v1/extensions
SALEOR_CLOUD_APP_DOMAIN=saleor.app
SKIP_SOURCEMAPS=true
```

`SALEOR_CLOUD_APP_DOMAIN=saleor.app` je upstream default na klasifikáciu aplikácií.
**Neznamená, že používaš Saleor Cloud.**

### 2.3 Pamäť pri builde

Stroj nemá swap a upstream `build` skript natvrdo žiada `--max-old-space-size=8192`,
čo je viac než voľná RAM. Bez ochrany môže OOM killer trafiť **produkčný gunicorn**.
Build preto vždy zavri do cgroup:

```bash
sudo systemd-run --scope --unit=dash-build \
  -p MemoryMax=3400M -p CPUQuota=180% --uid=ubuntu --gid=ubuntu \
  --setenv=NODE_OPTIONS=--max-old-space-size=3000 \
  --setenv=PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin \
  --working-directory=<build>/src \
  pnpm exec vite build
```

Pri 3.23.32 build prešiel za ~36 s bez OOM. Limity prispôsob aktuálnej voľnej RAM.

---

## 3. Postup: upgrade Core

Osvedčená sekvencia. **Odstávka bola 3 min 12 s (3.22.68) a 2 min 7 s (3.23.31).**

```
čistý upstream tag → iba stále potrebné MAKY zmeny → poetry lock → nový venv
→ preflight → krátke odstavenie → migrácie → prepnutie kódu a venv → read-back
```

### 3.1 Kandidát z čistého tagu

**Nikdy necherry-pickuj starý MAKY patch commit.** Vytvor worktree z čistého tagu
a ručne prenes len to, čo je v cieľovej verzii ešte potrebné.

Patch set pri 3.23.31 (2 súbory):
- `.gitignore` → `rsa_private.pem`
- `pyproject.toml` → `gunicorn>=23`, `setuptools>=80.9`, `vcrpy>=8,<9`, `pytest` (viď §3.3)

Pri každej verzii preveruj, či sú ešte potrebné:
- **gunicorn/setuptools** — upstream ich nedodáva, systemd ich potrebuje. Pravdepodobne áno.
- **vcrpy `>=8,<9`** — `vcrpy 7.0.0` pinuje `urllib3<2`, Saleor chce `>=2.7.0`, Poetry to nevyrieši. Mazať netreba, stačí bump.
- **pytest** — viď §3.3, kontroluj pri každom tagu.

### 3.2 Discount migrácie 0052–0055 — vyriešené, neotváraj

Pôvodný MAKY patch invertoval upstream migrácie: `0054` robil `DROP TABLE` namiesto
`state_operations`, `0052/0053/0055` boli prázdne. Django state si preto stále niesol
modely `Sale`, `SaleChannelListing`, `SaleTranslation`.

Opravené obnovením upstream súborov `0053`–`0055`. Do `3.22.68` bol potrebný
jednoriadkový backport `0052` (upstream `f47392d874`):

```sql
and constraint_type in ('FOREIGN KEY', 'UNIQUE')
```

**Od 3.23.31 vyššie tento backport netreba** — upstream ho už obsahuje. Over si to:

```bash
git show <TAG>:saleor/discount/migrations/0052_drop_sales_constraints.py | grep -c constraint_type
```

Overený stav: všetky štyri migrácie `applied` od 2026-03-19, `discount_sale*` tabuliek
je 0, `sale_id` stĺpce sú zachované na 3 tabuľkách, `makemigrations --check` vracia
`No changes detected`.

**Nikdy nepoužívaj `--fake`, needituj `django_migrations`, nevytváraj repair migration.**

### 3.3 ⚠️ Workaround: `pytest` v runtime závislostiach

V 3.23.31 produkčný kód importuje testovacie fixtures:

```
WebhookPlugin → webhook/payloads.py → core/utils/anonymization.py
→ core/utils/random_data.py:31 → account/tests/fixtures/__init__.py → import pytest
```

Bez `pytest` padne `manage.py check` aj **každý štart gunicornu a celery** na
`--only main` inštalácii. V 3.22.x tento import neexistuje.

Riešené pridaním `pytest` do runtime deps, **bez zmeny upstream zdrojáku**.

Pri ďalšom upgrade over, či to upstream opravil:

```bash
git show <TAG>:saleor/core/utils/random_data.py | grep -n 'tests.fixtures'
```

Ak už nie je → `pytest` z `pyproject.toml` odstráň. Ak stále je → nechaj.

### 3.4 Prenos kandidáta do /opt/saleor

Keďže `/opt/saleor` nemá MAKY remote (§1.1), použi bundle:

```bash
git -C <candidate> bundle create /tmp/cand.bundle <BRANCH>
sudo install -o saleor -g saleor -m 0644 /tmp/cand.bundle /opt/cand.bundle
sudo -u saleor git -C /opt/saleor fetch /opt/cand.bundle '<BRANCH>:<BRANCH>'
```

Toto sa dá spraviť **vopred**, mimo odstávky — fetch nemení working tree ani HEAD.

Ak bundle zlyhá s *„did not send all necessary objects"*, repozitár je shallow:

```bash
git -C <repo> rev-parse --is-shallow-repository
git -C <repo> fetch --unshallow upstream
```

Shallow hranica spôsobí, že tag vyzerá ako commit bez rodičov. **Nie je to orphan tag
ani nesúvisiaca história** — je to lokálny artefakt.

### 3.5 Preflight — toto je krok, ktorý sa vypláca

V dočasnom worktree s **novým** venv, kým produkcia ešte beží na starom:

```bash
python -c 'import saleor; print(saleor.__version__)'
manage.py check
manage.py migrate --plan          # skutočný plán, nie počet súborov v repo
manage.py makemigrations --check --dry-run
```

Preflight pri 3.23.31 zachytil `pytest` problém, ktorý by inak položil API
uprostred odstávky. **Nevynechávaj ho.**

### 3.6 Okno a read-back

Poradie: `beat` → `API` → (5 s) → `worker`. Fronty **nemaž**.
Po migráciách: prepni working tree, `daemon-reload`, štart `worker` → `beat` → `API`.

Read-back: verzia, tri služby active + `NRestarts=0`, produkt s obrázkom, SK cena a DPH,
**checkout vytvorený pred migráciou**, chyby v logoch, dokončenie reindexu.

### 3.7 Kompatibilitné prepínače — aktuálne hodnoty

```
use_legacy_shipping_zone_stock_availability = true    (DB stĺpec, nie Django setting)
allow_storefront_traffic                   = true
password_login_mode                        = enabled
account_confirm_merge_mode                 = merge_disabled
SEND_USAGE_TELEMETRY                       = False    (v .env, nastav PRED prvým štartom)
```

`SEND_USAGE_TELEMETRY` má v 3.23 default **True** a volá `usage-telemetry.saleor.io`
pri štarte každého workera. `merge_disabled` je zámerné — anonymné objednávky sa po
potvrdení účtu nepriradia, kým storefront nevie odovzdať heslo.

---

## 4. Postup: upgrade Dashboardu

```
tag → build na VPS (cgroup) → overenie index.html → lokálna záloha S3 prefixu
→ upload assetov → kontrola v S3 → index.html POSLEDNÝ → verejný read-back
```

Kľúčové pravidlá:
- **`index.html` vždy ako posledný.** Pred ním over cez `s3api head-object`, že všetky
  ním odkazované súbory už v S3 sú.
- **Žiadne `--delete`.** Staré hashované súbory nechaj — dobiehajúca cache ich žiada.
- Cache: assety aj HTML `public,max-age=60`. `index.html` explicitne
  `--content-type "text/html; charset=utf-8"`.
- Nemeň ACL, bucket policy, DNS ani CloudFront konfiguráciu.

### 4.1 Invalidácia CloudFrontu

EC2 rola **nemá** `cloudfront:CreateInvalidation`. Upload však funguje — sú to
oddelené oprávnenia.

Pri 3.23.32 invalidácia nebola potrebná: `index.html` má `max-age=60` a nové assety
majú nové hashe, takže nová verzia bola verejne živá ~30 s po uploade.

**Pri `AccessDenied` neopakuj build ani upload.** Over verejný výsledok. Ak treba
invalidovať, jeden príkaz v CloudShell:

```bash
aws cloudfront create-invalidation --distribution-id E5Y69895CVJ2P --paths '/*'
```

`InProgress` ≠ `COMPLETED`.

### 4.2 Overené hranice AWS oprávnení EC2 role

| Operácia | Výsledok |
|---|---|
| `s3 ls` / `cp` / `head-object` na `dashboard/` prefixe | **funguje** |
| `s3:ListAllMyBuckets` | AccessDenied |
| `cloudfront:ListDistributions` | AccessDenied |
| `cloudfront:CreateInvalidation` | AccessDenied |
| `rds:DescribeDBInstances` | AccessDenied |

**Zamietnutie výpisu všetkého nie je zamietnutie zápisu do známeho prefixu.** Posudzuj
konkrétnu operáciu na konkrétnom zdroji.

---

## 5. Storefront

| Položka | Hodnota |
|---|---|
| Adresár | `/opt/storefront` [z protokolu] |
| Framework | Next.js |
| Repozitár | `MakySto/maky-storefront` [z protokolu, neoverené] |
| Produkčný host / služba / deploy príkaz | **NOT_VERIFIED** |
| Pripravený commit | `30890123df3586d804c662dc6fd9fc77a78a8af2`, vetva `codex/saleor-3-23-storefront-compat` |
| Stav | **NENASADENÉ** |

Storefront **nie je na Saleor VPS** [overené — `/opt/storefront` tu neexistuje].
Má vlastné Claude Code vlákno s prístupom. Chýbajúce údaje doplní ono, nevymýšľaj ich.

Kontrola kompatibility s 3.23.31 prešla: žiadny GraphQL blocker, oba codegeny,
TypeScript, ESLint, build a 771/771 testov PASS.

Pripravená zmena `ResilientProductImage` rieši dočasné HTTP 503 na obrázkoch
(nové asynchrónne sťahovanie médií v 3.23) — po chybe zobrazí placeholder a raz
zopakuje `/thumbnail/` požiadavku po 3 s. Je to oprava UI, **nie** CFM media readiness.

**Storefront sa nenasadzuje do Dashboard S3 bucketu.** Používa vlastný deploy.

---

## 6. Rollback — tri oddelené domény

| Komponent | Čo sa vracia | Čo sa NEVRACIA |
|---|---|---|
| Core | kód, venv, systemd drop-iny, prípadne DB | Dashboard súbory, storefront |
| Dashboard | statické súbory v S3 prefixe | RDS, Core venv, checkouty |
| Storefront | jeho vlastný predchádzajúci release | Saleor DB, Dashboard bucket |

Zachované podklady [overené]:

```
/opt/saleor-rollback-3.22.50/    unit súbory, poetry.lock, pip freeze
/opt/saleor-rollback-3.22.68/    unit súbory, .env záloha, pip freeze
/home/ubuntu/dashboard-s3-backup-20260905T194403Z/
```

Plus staré vetvy a oba staršie venvy.

Návrat Core kódu = `git checkout` starej vetvy + prepnutie drop-inu + `daemon-reload`.
Pri 3.22.50 → 3.22.68 boli migrácie aditívne a starý kód ich toleroval.
**Pri návrate z 3.23 na 3.22 taká kompatibilita preukázaná NEBOLA** — 63 migrácií
späť nejde. Ber to ako núdzové riešenie.

Obnova DB je samostatný krok s konkrétnym bodom obnovy. Stratí objednávky a platobné
udalosti novšie než záloha. Nikdy nespúšťaj naslepo.

---

## 7. Čo NIE JE overené

Nezapisuj ako hotové len preto, že API vracia 200.

| Položka | Stav |
|---|---|
| Nasadenie storefront commitu `3089012` | **NENASADENÉ** |
| CFM write test s prísnym EditorJS parserom 3.23 | NOT_RUN — prvý veľký import bude prvá skúška |
| CFM asynchrónne médiá: pending/ready/failed + idempotentný druhý import | NOT_RUN |
| Nová objednávka + end-to-end platba | NOT_RUN (zámerne) |
| Plný nový delivery/checkout scenár s adresou | NOT_RUN |
| RDS bod obnovy a skúšobná obnova | NOT_VERIFIED — rola nemá `rds:DescribeDBInstances` |
| Detekcia zdieľaných hodnôt atribútov (upstream PR #19510) | NOT_RUN |
| **Rotácia uniknutého JWT RSA kľúča a RDS hesla** | **NOT_DONE — otvorené** |
| Push MAKY vetiev na GitHub | **NOT_DONE — viď §1.2** |

---

## 8. Poučenia — čo nerobiť

| Chybný predpoklad | Správne |
|---|---|
| „`/opt/saleor` má MAKY remote." | Má iba upstream Saleor. Prenášaj bundlom. |
| „Dashboard je v Core repe / aktualizuje sa s Pythonom." | Samostatný statický build na S3. |
| „Nie je vo `/var/www`, tak je to Vercel/Cloud." | Nie je. Poznáme presný S3 origin. |
| „Verejný `/index.js` = root bucketu." | CloudFront má `OriginPath /dashboard`. |
| „Stiahni build do PC a nahraj cez CloudShell." | VPS → S3 priamo cez IAM rolu. |
| „`ListAllMyBuckets` denied → upload nepôjde." | Iné oprávnenie. Skús konkrétnu operáciu. |
| „Invalidácia denied → zopakuj build." | Oddelené kroky. Over verejný read-back. |
| „Prenes celý starý patch commit." | Čistý tag + iba stále potrebné zmeny. |
| „Pole nie je v introspekcii → je odstránené." | Pridaj `includeDeprecated: true`. |
| „Počet migračných súborov = dĺžka odstávky." | Použi `migrate --plan` a veľkosť dát. |
| „Upstream komentár o pamäti = naša spotreba." | Zmeraj. 767 MB odhad vs. reálnych 300 MB. |
| „Build PASS = nasadené." | Commit ≠ build ≠ upload ≠ verejná verzia. |
| „Každý upgrade potrebuje nový AWS staging." | Existujúca infra + krátke okno. |
| „Poetry treba nahradiť uv kvôli upstreamu." | Zachovaj schválený nástroj. |
| „Spusti desiatky paralelných audit agentov." | Sekvenčná cielená práca. |

**Preflight sa vyplatil. Rozsiahle auditné kaskády a presúvanie artefaktov nie.**

---

## 9. Veľkosť dát (stav 2026-09-05)

| Tabuľka | Riadkov |
|---|---|
| `product_product` | 9 652 |
| `product_productvariant` | 9 652 |
| `attribute_attributevalue` | 2 151 |
| `checkout_checkout` | 21 |
| `order_order` | 18 |
| `page` / `payment` / `giftcard` | 0 |

Preto boli „ťažké" migrácie (reindex objednávok, checkoutov, giftcards) v praxi
triviálne. Jediná reálna záťaž bol product reindex: 9 652 produktov, ~300/min,
dobehol na pozadí, workery ~300 MB. **Pred ďalším zásahom zmeraj znova** — toto sú
čísla z jedného momentu, nie konštanty.

---

## 10. Úvodné zadanie pre ďalšie vlákno

```text
Pracuj podľa docs/operations/MAKY-DEPLOYMENT.md. Je to východiskový
záznam, nie príkaz nasadzovať historické verzie. Krátko porovnaj
aktuálny stav cieľovej súčasti so záznamom.

Cieľ: [Core / Dashboard / storefront + konkrétna zmena]
Rozsah: [iba príprava / výslovne schválené nasadenie]

Core: /opt/saleor, systemd, Poetry, samostatný venv.
      /opt/saleor NEMÁ MAKY remote — prenášaj git bundlom.
Dashboard: build na tomto VPS → priamy upload do
      s3://maky-store-static-936049945896/dashboard/
      CloudFront E5Y69895CVJ2P. Verejná cesta je /, nie /dashboard/.
Storefront: iný stroj, vlastný deploy. Nie Dashboard S3.

Bez subagentov a bez paralelných auditných kaskád.
Žiadny nový server, staging, Vercel, Saleor Cloud, zmena OS/DB.
Nemiešaj infra upgrade s modelovaním, novými SKU ani veľkým importom.
Nevypisuj tajomstvá. Neprenášaj staré migračné workaroundy naslepo.

Priprav kandidáta, urob preflight, zachovaj predchádzajúci runtime.
Pri už schválenom zásahu ho vykonaj; nežiadaj to isté GO znovu.
Zastav sa pri konkrétnom blockeri alebo chýbajúcom oprávnení.

Na konci doplň tento dokument: dátum, SHA, toolchain, migrácie,
výsledok nasadenia, verejnú verziu, zálohu a nevykonané testy.
```

---

## 11. Údržba dokumentu

Po každom zásahu aktualizuj §0–§2 (aktuálne verzie a SHA) a §7 (čo je stále neoverené).
Historickú časť neprepisuj.

Do repozitára **nepribaľuj** `.env`, obsah `/opt/saleor-rollback-*`, privátne kľúče,
DB connection stringy ani logy s osobnými údajmi.

Disk je na ~72 %. `/home/ubuntu/build-dashboard-3.23.32` má 1,1 GB (`node_modules`)
a po úspešnom nasadení sa dá zmazať.

---

## 12. B2B oslobodenie DPH (MAKY-B2B-V1) — 2026-11-02, NENASADENÉ

Vetva `maky-b2b-vat-exemption-v3.23.31` (nad `maky-production-patches-v3.23.31`).
Bez migrácií a nových závislostí; nový `.env` prepínač `MAKY_B2B_VAT_EXEMPTION_MODE`
(default `off`; `drain` iba pri príprave návratu) a `MAKY_B2B_PAYMENT_ATTEMPT_GRACE`
(default 5 min, dlhšie než gunicorn timeout). **Návrat na Core bez patchu** len po
`manage.py maky_b2b_rollback check` → `READY` (OPERATIONS §5), nikdy s otvorenými
B2B platbami. PR #16 obsahuje aj zosúladenie `uv.lock` s `pyproject.toml`
(gunicorn, setuptools, vcrpy 8 z §3.1) — iba lock pre CI, produkčný venv sa nemení.
Stripe app potrebuje webhook `TRANSACTION_CANCELATION_REQUESTED` (obnova zámku). Kontrakt: [MAKY-B2B-V1-CONTRACT.md](MAKY-B2B-V1-CONTRACT.md),
nasadenie/vypnutie/rollback: [MAKY-B2B-V1-OPERATIONS.md](MAKY-B2B-V1-OPERATIONS.md).
Po nasadení doplň §0–§1 (nový kódový SHA).
