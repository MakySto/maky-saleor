# MAKY-B2B-V1 — prevádzka, nasadenie, vypnutie, rollback (Core)

Doplnok k [MAKY-DEPLOYMENT.md](MAKY-DEPLOYMENT.md). Kontrakt pre storefront:
[MAKY-B2B-V1-CONTRACT.md](MAKY-B2B-V1-CONTRACT.md).

Stav: **pripravené, nenasadené.** Nič z tohto dokumentu sa nevykonáva bez
výslovného schválenia vlastníka.

## 1. Čo patch mení

| Oblasť | Miesto | Prečo pokrýva aj priame GraphQL volanie |
|---|---|---|
| Rozhodnutie a atómový zápis | `saleor/checkout/maky_b2b.py::apply_attestation`, mutácia `checkoutB2bTaxAttestationUpdate` | jediná cesta, ktorá zapisuje `maky.b2b.state.v1`; zamyká riadok checkoutu, jeho metadata a adresy; porovná verziu v tej istej transakcii |
| Cena pred návratom z cache | `saleor/checkout/calculations.py::_fetch_checkout_prices_if_expired` | každé čítanie ceny (API, dokončenie, platba, Celery) prechádza touto funkciou; ak natívny príznak nie je účinný, cache sa obíde a príznak sa opraví pod existujúcou `last_change` ochranou |
| Platobný snapshot | `transactionInitialize` → `maky_b2b.prepare_payment_attempt` | vykoná sa pred volaním platobnej aplikácie, z primárnej DB, s vynúteným prepočtom |
| Zámok zmien počas platby | `checkoutBillingAddressUpdate`, `checkoutShippingAddressUpdate`, `checkoutDeliveryMethodUpdate`, `updateMetadata`/`deleteMetadata` (vstupný kľúč), `taxExemptionManage`, samotná B2B mutácia | serverová kontrola v mutácii, nie v Next.js |
| Objednávka | `complete_checkout.py::_create_order` (legacy/bez platby) a `_create_order_from_checkout` (transakcie, automatické dokončenie, `orderCreateFromCheckout`) | obe cesty, ktorými vzniká objednávka z checkoutu |
| Rezervované metadata | `BaseMutation.create_metadata_from_graphql_input`, `deletePrivateMetadata`, `deleteMetadata` | generické mutácie nevedia zapísať ani zmazať `maky.b2b.*`; stav je navyše HMAC podpísaný (`SECRET_KEY`) |
| Natívne `taxExemptionManage` | `tax_exemption_manage.py` | na spravovanom checkoute `true` neobíde kontext/dôkaz/režim, `false` odvolá atestáciu |

Bežný B2C checkout (bez natívneho príznaku): **0 dotazov navyše** v cenovej ceste
(test `test_b2c_checkout_price_path_has_no_extra_query`), žiadne VIES volania.
Mutácie adries/doručenia: +1 indexovaný dotaz na metadata checkoutu.

**Migrácia:** žiadna (`makemigrations --check` → *No changes detected*). Nové
závislosti: žiadne. Ukladá sa do existujúcich `CheckoutMetadata.private_metadata`
a `Order.private_metadata`.

## 2. Konfigurácia (Core `.env`)

| Premenná | Default | Význam |
|---|---|---|
| `MAKY_B2B_VAT_EXEMPTION_MODE` | `off` | `on` povolí nové automatické oslobodenia. Iná hodnota = `off`. |
| `MAKY_B2B_PAYMENT_LOCK_TTL` | `20 minutes` | ako dlho drží začatý pokus o platbu **bez peňazí** daňový kontext zmrazený |

Nastavenie sa načíta pri štarte procesu. **Účinné až po reštarte `saleor`,
`saleor-celery` a `saleor-celerybeat`** (všetky musia mať rovnakú hodnotu).
Uloženie textu do `.env` samo nič nemení.

`SECRET_KEY` podpisuje B2B stav. Jeho zmena zneplatní rozpracované schválenia
(fail-safe: checkouty sa zdania, storefront ich musí znova overiť).

## 3. Nasadenie (návrh, čaká na schválenie)

Predpoklad: skontrolovať živý stav podľa MAKY-DEPLOYMENT §0 a §1 (živý SHA,
venv, služby). Nepredpokladať, že `91f50fc4b9` je stále živý kód.

1. **Kandidát:** commit tejto vetvy (rebase na živú líniu, ak sa líši) prenesený
   git bundle podľa MAKY-DEPLOYMENT §3.4. Zaznamenať pôvodný SHA pre rollback.
2. **Preflight** v dočasnom worktree s **existujúcim** venv (nové závislosti nie sú):
   `manage.py check`, `manage.py makemigrations --check --dry-run`,
   `manage.py migrate --plan` (očakávané: žiadne nové migrácie).
3. **`.env`:** nepridávať nič, alebo výslovne `MAKY_B2B_VAT_EXEMPTION_MODE=off`.
4. **Okno:** prepnúť working tree na kandidáta, reštart `saleor-celerybeat` →
   `saleor` → `saleor-celery` (rovnaký kód a politika vo všetkých procesoch).
5. **Read-back:** verzia, tri služby `active`, `NRestarts=0`; bežný checkout AT/DE
   vráti 154,98 € a `b2bTaxState.decision` (napr. `TAXED_NOT_COMPANY`); žiadne chyby
   v logu.
6. Storefront sa nasadzuje **až potom**, so svojím UI vypnutým.

Zapnutie (koordinovane, po akceptačných testoch v Stripe test mode a preflighte
storefrontu s tokenom): `MAKY_B2B_VAT_EXEMPTION_MODE=on` + reštart troch služieb, potom
storefront `B2B_CHECKOUT_UI_ENABLED=true`.

## 4. Vypnutie

1. `MAKY_B2B_VAT_EXEMPTION_MODE=off` + reštart troch služieb = autoritatívne
   zastavenie nových oslobodení. Storefront UI off len skryje vstup.
2. Nezaplatené spravované checkouty sa zdania pri najbližšom čítaní ceny
   (cache sa obíde, príznak sa opraví).
3. Checkouty s aktívnym platobným snapshotom dokončia platbu v zmrazenom režime
   (žiadne tiché navýšenie autorizovanej sumy).

## 5. Rollback na Core bez tohto patchu

Nie je to iba „checkout starého commitu a reštart“: starý kód by rešpektoval každý
natívny `tax_exemption=true` vrátane zastaraných B2B schválení.

1. Vypnúť nové oslobodenia (§4) a počkať, kým aktívne platby dobehnú
   (`MAKY_B2B_PAYMENT_LOCK_TTL` alebo ich dokončenie).
2. Odvolať spravované nezaplatené oslobodenia — read-only kontrola a potom cielený
   zápis (pripravené, spúšťať len so schválením):

   ```sql
   -- koľko je spravovaných checkoutov s natívnym príznakom
   SELECT c.token, c.last_change
   FROM checkout_checkout c
   JOIN checkout_checkoutmetadata m ON m.checkout_id = c.token
   WHERE c.tax_exemption
     AND m.private_metadata ? 'maky.b2b.state.v1';
   ```

   ```sql
   -- zrušenie natívneho príznaku + invalidácia cien (iba spravované checkouty)
   UPDATE checkout_checkout c
   SET tax_exemption = false, price_expiration = now(), last_change = now()
   FROM checkout_checkoutmetadata m
   WHERE m.checkout_id = c.token
     AND c.tax_exemption
     AND m.private_metadata ? 'maky.b2b.state.v1';
   ```
3. Overiť, že na spravovaných checkoutoch nezostal `tax_exemption=true`.
4. Až potom prepnúť kód späť a reštartovať tri služby.
5. Historické objednávky sa nemenia. Ich `maky.b2b.order.v1` zostáva ako záznam.

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
VIES záznam (`checkedAt`, `requestIdentifier`, `name`, `identity`), použitý platobný
snapshot a `sig` (HMAC; zmena záznamu je odhaliteľná). Neskoršia úprava profilu
zákazníka ani checkoutu tento záznam nemení. Záznam **nenahrádza** prepravné doklady
ani ostatné podmienky § 43.

## 7. Známe obmedzenia (konkrétne)

- Legacy platby (`checkoutPaymentCreate` + plugin brány) nemajú vlastný snapshot.
  MAKY ich podľa dostupných podkladov nepoužíva (NOT_VERIFIED na živom stroji);
  dokončenie aj tak vyžaduje plnú úhradu aktuálnej sumy a prejde kontrolou pri
  vytváraní objednávky.
- Zmena položiek alebo zľavy počas aktívnej platby nie je blokovaná; zachytí ju
  natívna kontrola `CHECKOUT_NOT_FULLY_PAID`.
- Uvoľnenie zámku bez peňazí: terminálna udalosť transakcie (`*_FAILURE`,
  `CANCEL_SUCCESS`) alebo TTL. Či Stripe app pri `transactionCancelationRequest` na
  nepotvrdenom PaymentIntente pošle `CANCEL_SUCCESS`, treba overiť v Stripe test mode.
  Ak neskorá autorizácia príde po uvoľnení a zmene kontextu, objednávka nevznikne
  (nedoplatok) a prostriedky uvoľní existujúci mechanizmus
  `release_funds_for_expired_checkouts`.
- `orderBulkCreate` (import) vie zapísať ľubovoľné privátne metadata objednávky;
  podpis `sig` odhalí, že záznam nevznikol v tomto workflow.
- Pri zápise atestácie sa neposiela webhook `CHECKOUT_UPDATED`.
- Daňový pôvod je napevno SK (`origin` v kontexte). Odosielanie z iného skladu
  krajiny nie je v MVP podporené.
- Stripe test-mode end-to-end priechod zatiaľ neprebehol (vyžaduje izolovanú Saleor
  inštanciu so Stripe app v test mode); automatické testy používajú mock platobnej
  aplikácie.
