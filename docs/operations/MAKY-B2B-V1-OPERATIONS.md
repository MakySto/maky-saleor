# MAKY-B2B-V1 — prevádzka, nasadenie, vypnutie, rollback (Core)

Doplnok k [MAKY-DEPLOYMENT.md](MAKY-DEPLOYMENT.md). Kontrakt pre storefront:
[MAKY-B2B-V1-CONTRACT.md](MAKY-B2B-V1-CONTRACT.md).

Stav: **pripravené, nenasadené** (PR #16, revízia 2). Nič z tohto dokumentu sa
nevykonáva bez výslovného schválenia vlastníka. Produkčný cleanup sa teraz
nevykonáva.

## 1. Čo patch mení

| Oblasť | Miesto | Prečo pokrýva aj priame GraphQL volanie |
|---|---|---|
| Rozhodnutie a atómový zápis | `saleor/checkout/maky_b2b.py::apply_attestation`, mutácia `checkoutB2bTaxAttestationUpdate` | jediná cesta, ktorá zapisuje `maky.b2b.state.v1`; zamyká riadok checkoutu, jeho metadata a adresy; porovná verziu v tej istej transakcii |
| Cena pred návratom z cache | `saleor/checkout/calculations.py::_fetch_checkout_prices_if_expired` | každé čítanie ceny (API, dokončenie, platba, Celery) prechádza touto funkciou; ak natívny príznak nie je účinný, cache sa obíde a príznak sa opraví pod existujúcou `last_change` ochranou |
| Platobný snapshot | `transactionInitialize` → `maky_b2b_payment.prepare_payment_attempt` | vykoná sa pred volaním platobnej aplikácie, pod zámkom checkoutu, z primárnej DB, s vynúteným prepočtom; uloží sumu, menu, kontext, tovar a kupón |
| Zámok zmien počas platby | `maky_b2b_guarded_change` v mutáciách adries, doručenia (`checkoutDeliveryMethodUpdate`, `checkoutShippingMethodUpdate`), položiek (`checkoutLinesAdd/Update/Delete`, `checkoutLineDelete`), kupónov (`checkoutAdd/RemovePromoCode`), `updateMetadata`/`deleteMetadata` (vstupný kľúč); `taxExemptionManage` | kontrola a **celý zápis** sú jedna transakcia pod zámkom riadku checkoutu (`guard_context_change` mimo transakcie vyhodí `RuntimeError`); `transactionInitialize` berie ten istý zámok |
| Stav pokusov o platbu | `maky_b2b.get_payment_attempts` | všetky transakcie checkoutu; čas pokus bez potvrdenia neukončí |
| Obnova | mutácia `checkoutB2bPaymentLockRelease` → `maky_b2b_payment.release_payment_attempts` | pokusy bez peňazí požiada o zrušenie cez platobnú app (`TRANSACTION_CANCELATION_REQUESTED`), pokusy s peniazmi nemení |
| Objednávka | `complete_checkout.py::_create_order` (legacy/bez platby) a `_create_order_from_checkout` (transakcie, automatické dokončenie, `orderCreateFromCheckout`) | obe cesty porovnajú vznikajúcu objednávku s potvrdenou ponukou (mena, suma, tovar, kupón, kontext) |
| Rezervované metadata | `BaseMutation.create_metadata_from_graphql_input`, `deletePrivateMetadata`, `deleteMetadata` | generické mutácie nevedia zapísať ani zmazať `maky.b2b.*`; stav je navyše HMAC podpísaný (`SECRET_KEY`) |
| Natívne `taxExemptionManage` | `tax_exemption_manage.py` | kontrola B2B stavu aj natívny zápis pod jedným zámkom; na spravovanom checkoute `true` neobíde kontext/dôkaz/režim, `false` odvolá atestáciu |

Bežný B2C checkout (bez natívneho príznaku): **0 dotazov navyše** v cenovej ceste
(test `test_b2c_checkout_price_path_has_no_extra_query`), žiadne VIES volania.
Mutácie adries, doručenia, položiek a kupónov: +2 indexované dotazy (zámok riadku
checkoutu, potom metadata; v jednom dotaze by čakajúca zmena čítala metadata spred
zápisu platby) a savepoint, ak už beží transakcia; zámok by aj tak vzal nasledný `UPDATE` checkoutu, drží sa len
počas zápisu (žiadne VIES/Stripe volanie v ňom). `transactionInitialize`: +1 krátka
transakcia so zámkom checkoutu.

**Migrácia:** žiadna (`makemigrations --check` → *No changes detected*). Nové
závislosti: žiadne. Ukladá sa do existujúcich `CheckoutMetadata.private_metadata`
a `Order.private_metadata`.

## 2. Konfigurácia (Core `.env`)

| Premenná | Default | Význam |
|---|---|---|
| `MAKY_B2B_VAT_EXEMPTION_MODE` | `off` | `on` povolí nové automatické oslobodenia. `drain` = `off` a navyše sa žiadny checkout nestane spravovaným (iba príprava rollbacku, §5). Iná hodnota = `off`. |
| `MAKY_B2B_PAYMENT_ATTEMPT_GRACE` | `5 minutes` | len pre pokus zo snapshotu, ku ktorému **nevznikla transakcia**: po tejto dobe platí, že Stripe nebol kontaktovaný (Saleor vytvára transakciu pred volaním app). Musí byť dlhšia než najdlhší možný request (`gunicorn --timeout`). Na pokus, ktorý Stripe dosiahol, nemá vplyv. |

`MAKY_B2B_PAYMENT_LOCK_TTL` z revízie 1 je zrušená (čas zámok neuvoľňuje).

Nastavenie sa načíta pri štarte procesu. **Účinné až po reštarte `saleor`,
`saleor-celery` a `saleor-celerybeat`** (všetky musia mať rovnakú hodnotu).
Uloženie textu do `.env` samo nič nemení; účinnú hodnotu over v bežiacom procese
(napr. `manage.py shell -c "from django.conf import settings; print(settings.MAKY_B2B_VAT_EXEMPTION_MODE)"`
v prostredí služby).

`SECRET_KEY` podpisuje B2B stav. Jeho zmena zneplatní rozpracované schválenia
(fail-safe: checkouty sa zdania, storefront ich musí znova overiť).

**Platobná app:** obnova potrebuje, aby Stripe app mala webhook
`TRANSACTION_CANCELATION_REQUESTED` (a oprávnenie `HANDLE_PAYMENTS`). Over v
Dashboarde → Apps → Stripe → Webhooks pred zapnutím B2B.

## 3. Nasadenie

**Vykonané 2026-11-03 v režime `off`** (commit `8ffed5e`); záznam a konkrétne príkazy
sú v MAKY-DEPLOYMENT §12.1. Postup pre ďalšie nasadenie:

Predpoklad: skontrolovať živý stav podľa MAKY-DEPLOYMENT §0 a §1 (živý SHA,
venv, služby). Nepredpokladať, že `91f50fc4b9` je stále živý kód.

1. **Kandidát:** commit tejto vetvy (rebase na živú líniu, ak sa líši) prenesený
   git bundle podľa MAKY-DEPLOYMENT §3.4. Zaznamenať pôvodný SHA pre rollback.
2. **Preflight** v dočasnom worktree s **existujúcim** venv (nové závislosti nie sú;
   zmena `uv.lock` len zosúlaďuje lock s `pyproject.toml`, ktorý už produkcia má):
   `manage.py check`, `manage.py makemigrations --check --dry-run`,
   `manage.py migrate --plan` (očakávané: žiadne nové migrácie).
3. **`.env`:** nepridávať nič, alebo výslovne `MAKY_B2B_VAT_EXEMPTION_MODE=off`.
4. **Okno:** prepnúť working tree na kandidáta, reštart `saleor-celerybeat` →
   `saleor` → `saleor-celery` (rovnaký kód a politika vo všetkých procesoch).
5. **Read-back:** verzia, tri služby `active`, `NRestarts=0`; bežný checkout AT/DE
   vráti 154,98 € a `b2bTaxState.decision` (napr. `TAXED_NOT_COMPANY`); žiadne chyby
   v logu.
6. Storefront sa nasadzuje **až potom**, so svojím UI vypnutým.

Zapnutie (koordinovane, po spoločnom teste v Stripe test mode §8 a preflighte
storefrontu s tokenom): `MAKY_B2B_VAT_EXEMPTION_MODE=on` + reštart troch služieb, potom
storefront `B2B_CHECKOUT_UI_ENABLED=true`. **Vykonané 2026-11-03 19:10 UTC**, na
Marekovo rozhodnutie bez spoločného testu §8; záznam v MAKY-DEPLOYMENT §12.2.

## 4. Vypnutie

Dve rôzne páky, nezamieňať (režim `drain` slúži iba na prípravu rollbacku, §5):

- **Storefront `B2B_CHECKOUT_UI_ENABLED=false`** iba skryje vstup a prestane volať
  B2B mutácie. **Neodvolá** oslobodenia už zapísané v Saleore.
- **Backend `MAKY_B2B_VAT_EXEMPTION_MODE=off`** + reštart troch služieb =
  autoritatívne zastavenie nových oslobodení:
  1. nové atestácie dostanú `TAXED_DISABLED`;
  2. nezaplatené spravované checkouty **bez aktívneho pokusu o platbu** sa zdania pri
     najbližšom čítaní ceny (cache sa obíde, príznak sa opraví);
  3. checkouty s aktívnym platobným snapshotom dokončia platbu v zmrazenom režime
     (žiadne tiché navýšenie autorizovanej sumy).

## 5. Rollback na Core bez tohto patchu

Starý Core nepozná B2B stav: rešpektoval by každý natívny `tax_exemption=true`
vrátane B2B schválení a nevidí platobné snapshoty ani kontrolu potvrdenej ponuky.
Preto platí jedno pravidlo: **kód sa prepne späť, až keď neexistuje žiadny
spravovaný checkout.** Kým má niektorý spravovaný checkout aktívny alebo
nevyjasnený pokus o platbu, zostáva nový Core (režim `drain`, inak `off`). Výnimky
pre „vedome ponechané platby“ neexistujú. Samotné čakanie nestačí. Postup najprv
over v izolovanom prostredí; **produkčný cleanup sa bez Marekovho schválenia
nespúšťa.**

Nástroj: `manage.py maky_b2b_rollback check|unmanage` (spúšťať v prostredí služby
`saleor`, s jej `.env`). Pokusy o platbu klasifikuje tá istá funkcia
`get_payment_attempts`, ktorá stráži zmeny checkoutu, takže inventúra vidí presne
to, čo backend: transakcie s peniazmi, transakcie bez peňazí bez `CANCEL_SUCCESS` /
plnej refundácie, aj pokus uložený v snapshote, ku ktorému ešte nevznikla
transakcia (počas `MAKY_B2B_PAYMENT_ATTEMPT_GRACE`). Zahŕňa všetky spravované
checkouty, aj tie s `tax_exemption=false` (napr. zdanené po VIES INVALID) a aj
stav s neplatným podpisom.

1. **Režim `drain`:** storefront `B2B_CHECKOUT_UI_ENABLED=false`; backend
   `MAKY_B2B_VAT_EXEMPTION_MODE=drain` + reštart `saleor-celerybeat` → `saleor` →
   `saleor-celery`; účinnú hodnotu over v bežiacom procese (§2). `drain` sa správa
   ako `off` a navyše **žiadny checkout sa nestane spravovaným** (atestácia na
   nespravovanom checkoute nič nezapíše a vráti `TAXED_DISABLED`). Platobný snapshot
   vzniká iba na spravovanom checkoute, takže množina spravovaných checkoutov odteraz
   iba klesá.
2. **Inventúra (read-only):** `manage.py maky_b2b_rollback check`. Vypíše režim a pre
   každý spravovaný checkout `tax_exemption`, rozhodnutie, platnosť podpisu,
   `attempts` (`FUNDS` / `UNRESOLVED` / `NONE`), transakcie s peniazmi a nevyjasnené
   transakcie (`id:pspReference`) a `being_created`. Výpis ulož ako podklad.
   Neobsahuje IČ DPH ani meno firmy.
3. **Vyriešiť platby** — iba ručne, po jednej, nič hromadne:
   - `FUNDS`: zákazník dokončí objednávku (zmrazená ponuka, kontrola pri dokončení
     platí), alebo po dohode zrušenie/refundácia v Dashboarde/Stripe. Žiadne
     automatické refundácie.
   - `UNRESOLVED` s transakciou: `checkoutB2bPaymentLockRelease` (B2B app token)
     alebo zrušenie PaymentIntentu v Stripe (§7); čakať na `CANCEL_SUCCESS`.
   - `UNRESOLVED` iba s `being_created`: počkať, kým request dobehne (najviac
     `MAKY_B2B_PAYMENT_ATTEMPT_GRACE`), a zopakovať inventúru.
4. **Cielené odpojenie:** `manage.py maky_b2b_rollback unmanage` (odmietne sa mimo
   režimu `drain`). Každý spravovaný checkout spracuje v **samostatnej krátkej
   transakcii pod zámkom riadku checkoutu**, tým istým, ktorý berie
   `transactionInitialize`: ak `get_payment_attempts` vráti iné než `NONE`, checkout
   ostane nezmenený (`BLOCKED`); inak odstráni B2B stav, odvolá natívne oslobodenie,
   ktoré nastavil workflow, a invaliduje ceny (`UNMANAGED`). Platba, ktorá súbežne
   začína, teda buď vidí obyčajný zdanený checkout, alebo checkout ostane `BLOCKED`.
   Nemení: transakcie, verejný vstup `maky.b2b.input.v1`, generické výnimky
   (`tax_exemption` bez B2B stavu), objednávky a ich `maky.b2b.order.v1`.
5. Kroky 2 až 4 opakovať, kým `check` neskončí `READY: no managed checkout is left.`
   (návratový kód 0; mimo `drain` alebo so zostávajúcim spravovaným checkoutom končí
   chybou `NOT READY`).
6. **Prepnúť kód späť** a reštartovať tri služby. Medzi krokom 5 a prepnutím
   nevznikne nový B2B pokus: v `drain` sa žiadny checkout nestane spravovaným a
   nespravovaný checkout snapshot nedostane. Nespravované platby počas prechodu sú
   bežné zdanené platby, ktoré starý Core spracuje ako doteraz. Počas postupného
   reštartu môžu chvíľu bežať staré procesy s `drain` a nové bez patchu; oba stavy
   sú bezpečné.
7. Ak sa v kroku 3 platbu vyriešiť nedá, návrat sa **nerobí**; zostáva nový Core
   s `drain` alebo `off`. Historické objednávky sa nemenia.

Krížová kontrola po kroku 5 (read-only, nezávislá od Pythonu):

```sql
SELECT count(*) FROM checkout_checkoutmetadata
WHERE private_metadata ? 'maky.b2b.state.v1'
   OR private_metadata ?| array['b2b_vies_valid', 'b2b_vat_country'];
-- očakávané: 0
```

## 6. Údaje pre ručnú faktúru

Dashboard → Playground (`<>`), staff s `MANAGE_ORDERS`:

```graphql
query OrderB2b($number: String!) {
  orders(first: 1, filter: { numbers: [$number] }) {
    edges { node {
      number
      total { net { amount currency } tax { amount } gross { amount } }
      billingAddress { companyName streetAddress1 postalCode city country { code } }
      shippingAddress { companyName streetAddress1 postalCode city country { code } }
      b2b: privateMetafield(key: "maky.b2b.order.v1")
      input: metafield(key: "maky.b2b.input.v1")
    } }
  }
}
```

`b2b` obsahuje `taxReason` (`EU_ICS_43` = § 43 oslobodené dodanie; `DOMESTIC_VAT`
= s DPH; `OUT_OF_SCOPE_EXPORT` = vývoz), `decision`, IČ DPH, IČO/DIČ, firmu, adresy,
VIES záznam (`checkedAt`, `requestIdentifier` ak ho VIES vrátil, `name`, `identity`),
použitý platobný snapshot a `sig` (HMAC; zmena záznamu je odhaliteľná). Neskoršia
úprava profilu zákazníka ani checkoutu tento záznam nemení. Záznam **nenahrádza**
prepravné doklady ani ostatné podmienky § 43.

## 7. Zaseknutý alebo nevyjasnený pokus o platbu

Príznak: `b2bTaxState.paymentLocked = true` dlhšie, `checkoutB2bPaymentLockRelease`
vracia `CANCELATION_REQUESTED` a nič sa nemení.

Zákazník nikdy nezostane bez možnosti pokračovať: **tú istú ponuku** môže vždy
zaplatiť znova, alebo začať nový košík. Na zmenu údajov treba pokus zosúladiť:

1. Nájdi transakcie checkoutu (Dashboard Playground, `HANDLE_PAYMENTS`):
   `checkout(id) { transactions { id pspReference events { type createdAt message } } }`.
2. V Stripe Dashboarde (správny režim live/test) vyhľadaj PaymentIntent podľa
   `pspReference`:
   - `succeeded` / `requires_capture`: peniaze existujú, Saleor ich ešte nevidí →
     počkaj na webhook Stripe app; zámok správne ostáva, objednávka sa dokončí
     s pôvodnou ponukou.
   - `requires_payment_method` / `requires_action` / `requires_confirmation`:
     zruš PaymentIntent v Stripe (*Cancel payment*). Stripe app pošle `CANCEL_SUCCESS`
     a zámok sa uvoľní.
   - `canceled`, ale v Saleore chýba `CANCEL_SUCCESS`: znova `checkoutB2bPaymentLockRelease`
     (po 60 s, kratšie opakovanie sa ignoruje); ak app udalosť nepošle, eskalovať —
     **nemeniť ručne `TransactionItem` ani metadata v DB.**
   - `pspReference` chýba (Stripe nevrátil odpoveď): vyhľadaj v Stripe podľa
     metadát transakcie/checkoutu; ak PaymentIntent neexistuje, zákazník pokračuje
     novým košíkom (starý košík nemôže prijať peniaze, pretože nič neexistuje).
3. Žiadne automatické refundácie ani sľub dodatočného vrátenia DPH.

## 8. Spoločný test so storefrontom (Stripe test mode, izolované prostredie)

Neprebehol. Vyžaduje izolovanú inštanciu Saleor (táto vetva) + Stripe app v test
mode + storefront PR #84 s `B2B_CHECKOUT_UI_ENABLED=true` a backend
`MAKY_B2B_VAT_EXEMPTION_MODE=on`. Produkcia nie je náhradou. Scenáre a očakávania:

| # | Scenár | Očakávanie |
|---|---|---|
| 1 | Bežný nákup DE (B2C) | 148,99 + 5,99 = 154,98 €, `TAXED_NOT_COMPANY`, objednávka bez `tax_exemption` |
| 2 | Firma DE, VIES VALID, doručenie DE | 121,13 + 4,87 = **126,00 €**, `EXEMPT`, PaymentIntent na 126,00, objednávka `taxReason=EU_ICS_43` |
| 3 | VIES nedostupný → „Pokračovať s DPH“ | `TAXED_UNAVAILABLE`, 154,98 €, žiadny sľub vrátenia |
| 4 | Začatá platba (karta `4000 0027 6000 3184`, 3DS nechať otvorené), v druhej karte zmena adresy | `B2B_CHECKOUT_LOCKED`, adresa nezmenená |
| 5 | To isté, zmena množstva / kupónu | `B2B_CHECKOUT_LOCKED` |
| 6 | Odmietnutá karta (`4000 0000 0000 0002`), potom „Zmeniť údaje“ | release → `CANCELATION_REQUESTED`; Stripe PI `canceled`; `CANCEL_SUCCESS` v Saleore; `paymentLocked=false`; zmena prejde |
| 7 | Odmietnutá karta, potom znova platba tej istej ponuky | `transactionInitialize` prejde (retry), 126,00 € |
| 8 | Nižšia suma po začatí platby (zníženie ceny variantu v Dashboarde) | `checkoutComplete` → `B2B_TAX_CONTEXT_CHANGED`, objednávka nevznikne, prostriedky sa uvoľnia (`release_funds_for_expired_checkouts`) |
| 9 | Vyššia suma po začatí platby | `checkoutComplete` → `B2B_TAX_CONTEXT_CHANGED` / `CHECKOUT_NOT_FULLY_PAID`, objednávka nevznikne |
| 10 | Overenie vyprší (24 h, `freeze`/posun času) počas začatej platby | platba dokončí oslobodenú ponuku (zmrazený snapshot) |
| 11 | `MAKY_B2B_VAT_EXEMPTION_MODE=off` počas začatej platby | platba dokončí zmrazenú ponuku; nový košík `TAXED_DISABLED` |
| 12 | Neskorý webhook (zastaviť Stripe CLI forwarding, potvrdiť platbu, po 30 min obnoviť) | zámok celý čas aktívny; po príchode peňazí objednávka s pôvodnou ponukou |
| 13 | Príprava rollbacku (§5): `drain`, otvorená platba z 4, `check` → `release` → `CANCEL_SUCCESS` → `unmanage` → `check` | prvý `check` `NOT READY` s `UNRESOLVED`; po zrušení `unmanage` vráti `UNMANAGED`, druhý `check` `READY`; nová atestácia checkout nespravuje |

Výsledky zapisovať oddelene od automatických testov (tie používajú mock platobnej
app a nič neposielajú do Stripe).

## 9. Známe obmedzenia (konkrétne)

- Legacy platby (`checkoutPaymentCreate` + plugin brány) nemajú vlastný snapshot.
  MAKY ich podľa dostupných podkladov nepoužíva (NOT_VERIFIED na živom stroji);
  aktívna legacy platba s peniazmi drží zámok a dokončenie prejde kontrolou ponuky.
- Zmenu ceny v katalógu alebo promo akcie (staff) počas platby backend nevie
  zablokovať; zachytí ju kontrola pri dokončení (objednávka nevznikne).
- Či Stripe app pri `TRANSACTION_CANCELATION_REQUESTED` na nepotvrdenom PaymentIntente
  odpovie `CANCEL_SUCCESS`, treba overiť v Stripe test mode (§8 scenár 6). Ak nie,
  platí ručný postup §7.
- Uvoľnenie zámku teraz vyžaduje akciu (release alebo zrušenie v Stripe). Neopustené
  košíky s nevyjasneným pokusom zostanú zamknuté pre B2B zmeny; zaplatenie tej istej
  ponuky aj nový košík fungujú.
- `orderBulkCreate` (import) vie zapísať ľubovoľné privátne metadata objednávky;
  podpis `sig` odhalí, že záznam nevznikol v tomto workflow.
- Pri zápise atestácie sa neposiela webhook `CHECKOUT_UPDATED`.
- Daňový pôvod je napevno SK (`origin` v kontexte). Odosielanie z iného skladu
  krajiny nie je v MVP podporené.
- Mutácie kupónov počítajú zľavu vnútri zámku; pri FLAT_RATES (MAKY) to neznamená
  žiadne externé volanie. Pri prechode na daňovú app by sa daňový webhook volal
  počas zámku.
