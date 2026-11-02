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
| `MAKY_B2B_VAT_EXEMPTION_MODE` | `off` | `on` povolí nové automatické oslobodenia. Iná hodnota = `off`. |
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

## 3. Nasadenie (návrh, čaká na schválenie)

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
storefront `B2B_CHECKOUT_UI_ENABLED=true`.

## 4. Vypnutie

Dve rôzne páky, nezamieňať:

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

Nie je to iba „checkout starého commitu a reštart“: starý kód by rešpektoval každý
natívny `tax_exemption=true` vrátane zastaraných B2B schválení a nevidel by platobné
snapshoty. Samotné čakanie (TTL) nestačí. Postup najprv over v izolovanom prostredí.

1. **Zastaviť nové oslobodenia:** §4 (backend `off` + reštart), storefront UI `off`.
2. **Inventúra** (read-only) — spravované checkouty s natívnym príznakom a stav ich
   pokusov o platbu:

   ```sql
   SELECT c.token,
          c.last_change,
          count(t.id)                                              AS transactions,
          count(t.id) FILTER (WHERE t.authorized_value > 0 OR t.charged_value > 0
                               OR t.authorize_pending_value > 0
                               OR t.charge_pending_value > 0)      AS with_money,
          count(t.id) FILTER (WHERE t.authorized_value = 0 AND t.charged_value = 0
                               AND t.authorize_pending_value = 0
                               AND t.charge_pending_value = 0
                               AND NOT EXISTS (
                                 SELECT 1 FROM payment_transactionevent e
                                 WHERE e.transaction_id = t.id
                                   AND e.type IN ('cancel_success', 'refund_success')))
                                                                   AS unresolved
   FROM checkout_checkout c
   JOIN checkout_checkoutmetadata m ON m.checkout_id = c.token
   LEFT JOIN payment_transactionitem t ON t.checkout_id = c.token
   WHERE c.tax_exemption
     AND m.private_metadata ? 'maky.b2b.state.v1'
   GROUP BY c.token, c.last_change
   ORDER BY with_money DESC, unresolved DESC;
   ```

   Zoznam ulož (token, počty) ako podklad rozhodnutia.
3. **Vyriešiť platby** — pre každý riadok:
   - `with_money > 0`: nechať zákazníka dokončiť objednávku (zmrazený režim), alebo
     po dohode zrušiť/refundovať v Dashboarde/Stripe. **Nezahŕňať do cleanupu.**
   - `unresolved > 0`: `checkoutB2bPaymentLockRelease` (B2B app token) alebo zrušenie
     PaymentIntentu v Stripe (§7); počkať na `CANCEL_SUCCESS`.
   - Inventúru (krok 2) zopakovať, kým sú v cleanupe len riadky s `with_money = 0` a
     `unresolved = 0`.
4. **Cielený cleanup** — iba spravované checkouty bez peňazí a bez nevyjasneného
   pokusu; generické výnimky (bez `maky.b2b.state.v1`) a historické objednávky sa
   nemenia:

   ```sql
   UPDATE checkout_checkout c
   SET tax_exemption = false, price_expiration = now(), last_change = now()
   FROM checkout_checkoutmetadata m
   WHERE m.checkout_id = c.token
     AND c.tax_exemption
     AND m.private_metadata ? 'maky.b2b.state.v1'
     AND NOT EXISTS (
       SELECT 1 FROM payment_transactionitem t
       WHERE t.checkout_id = c.token
         AND (t.authorized_value > 0 OR t.charged_value > 0
              OR t.authorize_pending_value > 0 OR t.charge_pending_value > 0
              OR NOT EXISTS (
                SELECT 1 FROM payment_transactionevent e
                WHERE e.transaction_id = t.id
                  AND e.type IN ('cancel_success', 'refund_success'))));
   ```
5. **Overiť:** inventúra (krok 2) neobsahuje riadok s `with_money = 0` a
   `unresolved = 0`; zostávajúce riadky sú vedome ponechané platby.
6. Až potom prepnúť kód späť a reštartovať tri služby.
7. Historické objednávky sa nemenia. Ich `maky.b2b.order.v1` zostáva ako záznam.

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
