# MAKY-B2B-V1 — kontrakt backend ↔ storefront

**Stav:** implementované v `maky-saleor`, vetva `maky-b2b-vat-exemption-v3.23.31`
(základ `maky-production-patches-v3.23.31`), PR #16 zlúčený ako `8ffed5e`.
**Nasadené 2026-11-03 v režime `off`** (backend; B2B sa zatiaľ nikomu neponúka).
**Revízia 2** (opravy po posudku, 2. 11. 2026) — zmeny oproti commitu `68ca91a` sú
zhrnuté v §0. Názvy a význam existujúcich polí sa nemenia.
**Pre koho:** relácia *MAKY.STORE - Storefront 4.5*. Toto je jediný záväzný opis
nového GraphQL rozhrania. Zmena názvov alebo významu sa zapisuje sem aj do
storefront handoffu.

Všetko nové je **fork-only** (nie je v upstream Saleore).

## 0. Zmeny v revízii 2 (pre storefront)

Nič sa nepremenovalo: `Checkout.b2bTaxState`, typ `B2bTaxState`, `contextVersion`,
`paymentLocked`, `checkoutB2bTaxAttestationUpdate`, jej vstupy, rozhodnutia a chybové
kódy zostávajú. Zmenilo sa správanie platobného zámku a pribudla jedna mutácia:

| # | Zmena | Čo s tým storefront |
|---|---|---|
| 1 | Zámok sa **neuvoľní časom** ani udalosťou `*_FAILURE`. Uvoľní ho iba potvrdené zrušenie pokusu (`CANCEL_SUCCESS`), plná refundácia, alebo dôkaz, že pokus vôbec nevznikol. | Po zlyhanej karte zákazník môže zaplatiť **tú istú ponuku** znova (`transactionInitialize` prejde). Ak chce meniť údaje, musí najprv uvoľniť zámok (bod 2). |
| 2 | Nová mutácia `checkoutB2bPaymentLockRelease(id)` (rovnaký app token). Pokusy bez peňazí požiada platobnú aplikáciu zrušiť; pokusy s peniazmi nechá tak. Vráti `status`. | Tlačidlo „Zmeniť údaje“ v stave `paymentLocked`: zavolať mutáciu, pri `CANCELATION_REQUESTED` / `ATTEMPT_BEING_CREATED` krátko pollovať `b2bTaxState.paymentLocked`, kým nie je `false`. Pri `PAYMENT_HAS_FUNDS` zmenu neponúkať, nechať dokončiť platbu. |
| 3 | `paymentLocked` je `true` aj na **nespravovanom** checkoute, ak má pokus o platbu, ktorý ešte môže zobrať peniaze. Atestácia je vtedy odmietnutá (`CHECKOUT_LOCKED`). | Rovnaké správanie ako doteraz pri `paymentLocked=true`: neposielať VIES ani atestáciu, ponúknuť bod 2. |
| 4 | Počas aktívneho snapshotu sú odmietnuté aj zmeny **položiek, množstva a kupónov** (`checkoutLinesAdd/Update/Delete`, `checkoutLineDelete`, `checkoutAddPromoCode`, `checkoutRemovePromoCode`) a zastaraná `checkoutShippingMethodUpdate`: `CheckoutErrorCode.B2B_CHECKOUT_LOCKED`. | Mapovať rovnako ako pri adrese: „Platba už prebieha“ + bod 2. Pri kupónoch je `field` = `promoCode`. |
| 5 | `checkoutComplete` porovná objednávku s potvrdenou ponukou (mena, suma, tovar, kupón, daňový kontext). Nezhoda → `B2B_TAX_CONTEXT_CHANGED` aj vtedy, keď je zaplatené **viac**. | Zobraziť „Suma sa zmenila, objednávka nevznikla“; peniaze uvoľní existujúci mechanizmus Saleoru. |
| 6 | Kontrola zámku a zápis zmeny sú jedna transakcia pod zámkom checkoutu. | Nič; súbežná platba buď vidí nové údaje, alebo je zmena odmietnutá. |
| 7 | Premenná `MAKY_B2B_PAYMENT_LOCK_TTL` zrušená, nahradila ju `MAKY_B2B_PAYMENT_ATTEMPT_GRACE` (backend). | Nič. |
| 8 | Nový režim backendu `drain` (iba príprava návratu na starý Core). Atestácia na nespravovanom checkoute vtedy nič nezapíše a vráti `TAXED_DISABLED`, bez chyby. | Nič; storefront UI je v tom čase vypnuté. |

**Čo musí storefront doplniť pred zapnutím B2B.** Schéma je spätne kompatibilná:
existujúce volania fungujú bez zmeny a storefront s vypnutým B2B UI možno nasadiť.
Pred zapnutím (`B2B_CHECKOUT_UI_ENABLED=true`) však storefront **musí** dopojiť
obnovu po neúspešnej platbe podľa bodu 2: v stave `paymentLocked=true` akciu „Zmeniť
údaje“, serverové volanie `checkoutB2bPaymentLockRelease` B2B app tokenom (token
zostáva na serveri), spracovanie `RELEASED` / `CANCELATION_REQUESTED` /
`ATTEMPT_BEING_CREATED` / `PAYMENT_HAS_FUNDS` a povolenie zmeny údajov až po
`paymentLocked=false`. Bez toho zákazník po odmietnutej karte nemôže zmeniť adresu
(backend zmenu správne odmietne). Žiadna automatická refundácia ani automatický nový
košík či nová platba. Komentáre a testy, ktoré počítajú s uvoľnením zámku časom
(revízia 1: 20 minút), treba odstrániť.

## 1. Čo storefront robí a čo nie

| Storefront | Backend |
|---|---|
| zapisuje verejné metadata `maky.b2b.input.v1` (zákaznícky vstup) | nikdy z neho sám neudelí oslobodenie |
| číta `checkout.b2bTaxState` (verejne, rovnako ako ostatné polia checkoutu) | počíta stav spolu s cenami tej istej odpovede |
| volá VIES na serveri, **pred** mutáciou | VIES nevolá, počas VIES nedrží zámky |
| volá `checkoutB2bTaxAttestationUpdate` serverovým tokenom `SALEOR_B2B_APP_TOKEN` | overí aktuálnosť, uloží atestáciu, nastaví natívny príznak, invaliduje ceny — v jednej transakcii |
| zobrazuje sumy zo Saleoru | počíta natívne (FLAT_RATES), žiadne delenie 1,23 |

Storefront **nesmie**: posielať `exempt: true`, volať `taxExemptionManage`,
zapisovať privátne metadata, deliť ceny 1,23, porovnávať text chýb.

## 2. Zákaznícky vstup (verejné metadata checkoutu)

Kľúč `maky.b2b.input.v1`, hodnota je JSON reťazec:

```json
{"schema": 1, "isBusiness": true, "registrationId": "HRB 12345", "taxId": "", "vatId": "DE123456789"}
```

- `schema` musí byť `1`, `isBusiness` musí byť JSON boolean (nie `"true"`),
  `registrationId`/`taxId`/`vatId` reťazce max. 64 znakov. Inak `updateMetadata`
  vráti `MetadataErrorCode.INVALID`.
- `vatId` posielaj celé, normalizované: veľké písmená, bez medzier, bodiek a pomlčiek,
  s prefixom krajiny (Grécko `EL`). Backend normalizuje rovnako a pri atestácii
  vyžaduje **presnú zhodu**.
- Obchodné meno a fakturačná adresa = `checkout.billingAddress` (vrátane
  `companyName`). Iný zdroj nie je.
- Zápis/zmazanie tohto kľúča **invaliduje ceny** a pri aktívnej platbe je odmietnutý
  (`MetadataErrorCode.B2B_CHECKOUT_LOCKED`).
- Ostatné kľúče `maky.b2b.*` sú rezervované: verejné aj privátne zápisy cez generické
  metadata mutácie, `checkoutCreate`, `checkoutComplete`, `orderCreateFromCheckout`
  vrátia `INVALID`. `checkoutComplete(metadata:)` nesmie obsahovať ani vstupný kľúč.

## 3. SDL (vygenerované z `saleor/graphql/schema.graphql`)

```graphql
type Checkout {
  # ...existujúce polia...
  b2bTaxState: B2bTaxState!
}

type B2bTaxState {
  contextVersion: String!          # nepriehľadné, len prenášať
  decision: B2bTaxDecision!
  effectiveTaxExemption: Boolean!  # MAKY § 43 je účinné pre ceny tejto odpovede
  checkedAt: DateTime              # pôvodný čas uloženého VIES overenia
  expiresAt: DateTime              # len pri VALID: checkedAt + 24 h
  paymentLocked: Boolean!          # pokus o platbu drží alebo ešte môže zobrať peniaze (§8)
}

enum B2bTaxDecision {
  EXEMPT
  TAXED_NOT_COMPANY
  TAXED_NO_VAT_ID
  TAXED_SK_VAT
  TAXED_DOMESTIC_DELIVERY
  TAXED_COUNTRY_MISMATCH
  TAXED_UNVERIFIED
  TAXED_INVALID
  TAXED_UNAVAILABLE
  TAXED_IDENTITY_REVIEW
  TAXED_STALE
  TAXED_DISABLED
  OUT_OF_SCOPE
}

enum B2bVatVerificationStatus { VALID INVALID UNAVAILABLE }

input CheckoutB2bVatVerificationInput {
  vatId: String!
  status: B2bVatVerificationStatus!
  checkedAt: DateTime!             # pôvodný čas VIES, aj pri opakovanom použití cache
  requestIdentifier: String        # len ak ho VIES vrátil; nevymýšľať
  name: String
  address: String
}

input CheckoutB2bTaxAttestationInput {
  expectedContextVersion: String!
  verification: CheckoutB2bVatVerificationInput   # null = odvolať schválenie
}

type Mutation {
  checkoutB2bTaxAttestationUpdate(id: ID!, input: CheckoutB2bTaxAttestationInput!): CheckoutB2bTaxAttestationUpdate
  checkoutB2bPaymentLockRelease(id: ID!): CheckoutB2bPaymentLockRelease   # revízia 2
}

type CheckoutB2bPaymentLockRelease {
  checkout: Checkout
  status: B2bPaymentLockReleaseStatus
  errors: [CheckoutB2bPaymentLockReleaseError!]!
}

enum B2bPaymentLockReleaseStatus {
  RELEASED                 # nič už nemôže zobrať peniaze; zmeny sú povolené
  CANCELATION_REQUESTED    # zrušenie vyžiadané od platobnej app; čakať na paymentLocked=false
  ATTEMPT_BEING_CREATED    # transactionInitialize ešte beží; skúsiť o chvíľu
  PAYMENT_HAS_FUNDS        # peniaze sú autorizované/zúčtované; dokončiť platbu
}

type CheckoutB2bPaymentLockReleaseError {
  field: String
  message: String
  code: CheckoutB2bPaymentLockReleaseErrorCode!
}

enum CheckoutB2bPaymentLockReleaseErrorCode { GRAPHQL_ERROR NOT_FOUND }

type CheckoutB2bTaxAttestationUpdate {
  checkout: Checkout
  errors: [CheckoutB2bTaxAttestationError!]!
}

type CheckoutB2bTaxAttestationError {
  field: String
  message: String
  code: CheckoutB2bTaxAttestationErrorCode!
}

enum CheckoutB2bTaxAttestationErrorCode {
  GRAPHQL_ERROR
  INVALID
  NOT_FOUND
  STALE_CONTEXT
  INVALID_ATTESTATION
  CHECKOUT_LOCKED
  UNMANAGED_EXEMPTION_CONFLICT
}
```

Nové hodnoty v **existujúcich** enumoch (frontend mapuje `code`, nie text):

| Enum | Nové hodnoty | Kde |
|---|---|---|
| `CheckoutErrorCode` | `B2B_CHECKOUT_LOCKED`, `B2B_TAX_CONTEXT_CHANGED` | zámok: `checkoutBillingAddressUpdate`, `checkoutShippingAddressUpdate`, `checkoutDeliveryMethodUpdate`, `checkoutShippingMethodUpdate`, `checkoutLinesAdd`, `checkoutLinesUpdate`, `checkoutLinesDelete`, `checkoutLineDelete`, `checkoutAddPromoCode`, `checkoutRemovePromoCode`; `checkoutComplete` (objednávka ≠ potvrdená ponuka) |
| `TransactionInitializeErrorCode` | `B2B_CHECKOUT_LOCKED`, `B2B_TAX_CONTEXT_CHANGED` | `transactionInitialize` |
| `MetadataErrorCode` | `B2B_CHECKOUT_LOCKED` | `updateMetadata`/`deleteMetadata` vstupného kľúča |
| `TaxExemptionManageErrorCode` | `B2B_CHECKOUT_LOCKED`, `B2B_MANAGED_CHECKOUT` | `taxExemptionManage` na spravovanom checkoute |
| `OrderCreateFromCheckoutErrorCode` | `INVALID`, `B2B_TAX_CONTEXT_CHANGED` | staff/app `orderCreateFromCheckout` |
| `PaymentErrorCode` | `B2B_CHECKOUT_LOCKED`, `B2B_TAX_CONTEXT_CHANGED` | rezervované (legacy platby MAKY nepoužíva) |

## 4. Oprávnenia a token

`checkoutB2bTaxAttestationUpdate` aj `checkoutB2bPaymentLockRelease` povolia iba **aktívnu aplikáciu** s **oboma**
oprávneniami `MANAGE_TAXES` **a** `HANDLE_TAXES` (AND je vynútené v kóde; staff
používateľ ani app s jedným z nich neprejde → GraphQL `PermissionDenied`).
Token: `SALEOR_B2B_APP_TOKEN` iba na serveri storefrontu. `b2bTaxState` je čitateľné
bez tokenu každým, kto smie čítať daný checkout; neobsahuje privátny audit.

## 5. Postup v storefronte

```
1. query checkout { b2bTaxState { contextVersion decision ... } totalPrice {...} }
2. ak decision je TAXED_UNVERIFIED / TAXED_STALE / TAXED_UNAVAILABLE a vstup je firemný
   a zahraničný: server zavolá VIES (bez držania čohokoľvek v Saleore)
3. mutation checkoutB2bTaxAttestationUpdate(expectedContextVersion = verzia z kroku 1)
4. STALE_CONTEXT -> späť na krok 1 (nová verzia), VIES výsledok z cache možno
   použiť znova s PÔVODNÝM checkedAt
5. zobraz totalPrice + b2bTaxState z odpovede mutácie; zákazník potvrdí sumu
6. transactionInitialize (amount = potvrdená suma alebo vynechané)
7. checkoutComplete
```

`verification: null` = odvolanie (napr. zákazník vypol nákup na firmu). Aj odvolanie
potrebuje aktuálnu verziu.

Obnova po začatej platbe (revízia 2), keď `paymentLocked = true` a zákazník chce meniť
údaje:

```
a. mutation checkoutB2bPaymentLockRelease(id)            (serverový app token)
b. RELEASED              -> pokračovať krokom 1 (zmena údajov je povolená)
   CANCELATION_REQUESTED -> pollovať b2bTaxState.paymentLocked (napr. 1 s, max ~30 s);
                            false -> krok 1; stále true -> ponúknuť zaplatiť pôvodnú
                            ponuku alebo kontakt na obchod (manuálne zosúladenie, OPERATIONS §7)
   ATTEMPT_BEING_CREATED -> zopakovať a. o niekoľko sekúnd
   PAYMENT_HAS_FUNDS     -> zmenu neponúkať; dokončiť objednávku s pôvodnou ponukou
```

Zaplatiť **tú istú** ponuku znova (napr. po odmietnutej karte) je možné vždy bez kroku a.

```graphql
mutation ReleaseB2bPayment($id: ID!) {
  checkoutB2bPaymentLockRelease(id: $id) {
    status
    errors { field code message }
    checkout { b2bTaxState { contextVersion decision paymentLocked } }
  }
}
```

### Príklad: dotaz

```graphql
query B2bState($id: ID!) {
  checkout(id: $id) {
    b2bTaxState { contextVersion decision effectiveTaxExemption checkedAt expiresAt paymentLocked }
    totalPrice { net { amount currency } tax { amount currency } gross { amount currency } }
  }
}
```

### Príklad: mutácia

```graphql
mutation ApplyB2bVerification($id: ID!, $input: CheckoutB2bTaxAttestationInput!) {
  checkoutB2bTaxAttestationUpdate(id: $id, input: $input) {
    errors { field code message }
    checkout {
      id
      b2bTaxState { contextVersion decision effectiveTaxExemption checkedAt expiresAt }
      totalPrice { net { amount currency } tax { amount currency } gross { amount currency } }
    }
  }
}
```

```json
{
  "id": "Q2hlY2tvdXQ6...",
  "input": {
    "expectedContextVersion": "MToxOjk0ZDY...",
    "verification": {
      "vatId": "DE123456789",
      "status": "VALID",
      "checkedAt": "2026-11-02T14:00:00+00:00",
      "requestIdentifier": "WAPIAAAAZ1a2b3c4",
      "name": "---",
      "address": "---"
    }
  }
}
```

Úspech (overené testom `test_apply_valid_attestation`, 148,99 + 5,99 €):

```json
{
  "errors": [],
  "checkout": {
    "b2bTaxState": {"decision": "EXEMPT", "effectiveTaxExemption": true,
                    "checkedAt": "2026-11-02T14:00:00+00:00", "expiresAt": "2026-11-03T14:00:00+00:00"},
    "totalPrice": {"net": {"amount": 126.0}, "tax": {"amount": 0.0}, "gross": {"amount": 126.0}}
  }
}
```

Pred schválením ten istý checkout: `gross 154.98, net 126.00, tax 28.98`.

Chyba (zmena v druhej karte medzi čítaním verzie a mutáciou):

```json
{"errors": [{"field": "expectedContextVersion", "code": "STALE_CONTEXT",
             "message": "Checkout context changed. Reload b2bTaxState and retry."}],
 "checkout": null}
```

## 6. Význam rozhodnutí

| `decision` | Kedy | Čo urobí storefront |
|---|---|---|
| `EXEMPT` | firma, platné zahraničné EÚ VAT (VIES VALID ≤ 24 h), doručenie do krajiny VAT, režim `on` | zobrazí cenu bez DPH zo Saleoru |
| `TAXED_NOT_COMPANY` | `isBusiness` nie je `true` | bežný nákup |
| `TAXED_NO_VAT_ID` | firma bez IČ DPH | firma s DPH |
| `TAXED_SK_VAT` | slovenské IČ DPH | firma s DPH |
| `TAXED_DOMESTIC_DELIVERY` | doručenie/odber na Slovensku (aj osobný odber) | firma s DPH |
| `TAXED_COUNTRY_MISMATCH` | krajina VAT ≠ krajina doručenia | s DPH + možnosť kontaktovať obchod (nejde o zákonný zákaz) |
| `TAXED_UNVERIFIED` | chýba overenie alebo úplný kontext (adresa, firma, neplatný formát VAT) | spustiť/dokončiť overenie |
| `TAXED_INVALID` | VIES vrátil neplatné číslo | s DPH, „IČ DPH nie je platné“ |
| `TAXED_UNAVAILABLE` | VIES nedostupný | skúsiť neskôr alebo pokračovať s DPH (bez sľubu vrátenia) |
| `TAXED_IDENTITY_REVIEW` | VIES vrátil meno, ktoré sa nezhoduje s firmou | s DPH, kontakt na obchod |
| `TAXED_STALE` | overenie nesedí na aktuálne údaje alebo vypršalo | znova overiť (nová verzia) |
| `TAXED_DISABLED` | backend `MAKY_B2B_VAT_EXEMPTION_MODE=off` alebo `drain` | s DPH |
| `OUT_OF_SCOPE` | doručenie mimo EÚ (US/CA) | existujúci vývozný režim, nič neoverovať |

Poradie vyhodnotenia: mimo EÚ → firma → VAT → SK VAT → formát VAT → domáce
doručenie/odber → úplnosť → zhoda krajín → režim → atestácia (zhoda VAT a kontextu,
INVALID/UNAVAILABLE, identita, 24 h).

Identita: VIES názov `---`/prázdny **nie je** negatívny výsledok. Použiteľný názov sa
porovná deterministicky (bez diakritiky, interpunkcie, veľkosti písmen a právnych
foriem typu GmbH/s.r.o./AG); zhoda = všetky slová kratšieho názvu sú v dlhšom.

## 7. Kontext a verzia

`contextVersion` sa mení pri zmene čohokoľvek z: checkout, kanál, mena, verzia
politiky, celý vstup `maky.b2b.input.v1`, všetky polia fakturačnej aj dodacej adresy
(meno, firma, ulica, mesto, PSČ, krajina, oblasť), druh doručenia (adresa vs. konkrétny
odberný bod) — a pri každom zápise atestácie (revízia). Konkrétny kuriér sa do
kontextu nepočíta (nemení daňový režim), takže zmena kuriéra overenie nezneplatní.
Zmena množstva alebo zľavy kontext nemení, ale ceny prepočíta bežná logika Saleoru.

## 8. Platba

- `transactionInitialize`: pod zámkom checkoutu (aj na nespravovanom checkoute, aby
  sa nestihla vložiť atestácia) prepočíta ceny z primárnej DB. Na spravovanom checkoute
  posúdi atestáciu (24 h sa hodnotí tu) a uloží **nemenný platobný snapshot**
  (rozhodnutie, suma, mena, daňový kontext, tovar = množstvo na variant, kupón)
  naviazaný na `idempotencyKey` pokusu, **ešte pred** volaním Stripe app.
- `amount`: vynechané = suma zo servera. Zadané a odlišné od servera →
  `B2B_TAX_CONTEXT_CHANGED` (pole `amount`), Stripe sa nevolá. Storefront obnoví
  rekapituláciu a nechá sumu potvrdiť znova.
- Opakované `transactionInitialize` s rovnakou ponukou (kontext, suma, mena, tovar,
  kupón) sa pripojí k tomu istému snapshotu. Iná ponuka počas aktívneho snapshotu →
  `B2B_CHECKOUT_LOCKED`.
- Počas aktívneho snapshotu: zmena adries, doručenia, vstupu, atestácie, položiek,
  množstva a kupónov → `B2B_CHECKOUT_LOCKED`. Toto je stav platby, nie neplatná adresa.
  Kontrola a zápis prebehnú v jednej transakcii pod zámkom checkoutu.
- **Kedy je zámok aktívny (`paymentLocked = true`):** kým niektorý pokus o platbu
  checkoutu (všetky transakcie checkoutu, nielen tie zo snapshotu) drží peniaze
  (autorizované, zúčtované, čakajúce) **alebo ich ešte môže zobrať**. Pokus bez peňazí
  je ukončený iba po `CANCEL_SUCCESS`, po plnej refundácii, pri internej darčekovej
  karte po jej zlyhaní, alebo ak transakcia k pokusu zo snapshotu nevznikla ani po
  `MAKY_B2B_PAYMENT_ATTEMPT_GRACE` (default 5 min; dovtedy `ATTEMPT_BEING_CREATED`).
  **Čas ani `AUTHORIZATION_FAILURE`/`CHARGE_FAILURE` zámok neuvoľnia** — Stripe
  PaymentIntent po zlyhaní možno potvrdiť znova.
- Uvoľnenie: `checkoutB2bPaymentLockRelease` (§5). Nikdy nerobí refundáciu ani
  nezruší autorizované peniaze.
- Počas aktívneho snapshotu platí jeho daňový režim aj po vypršaní VIES alebo po
  vypnutí režimu (žiadne tiché navýšenie autorizovanej sumy).
- `checkoutComplete` / `orderCreateFromCheckout` pri aktívnom snapshote porovnajú
  objednávku, ktorá práve vzniká, s potvrdenou ponukou: mena, celková suma (pred
  legacy darčekovými kartami), tovar, kupón, daňový kontext. Nezhoda →
  `B2B_TAX_CONTEXT_CHANGED` (aj pri **nižšej** sume, ktorú by natívna kontrola úhrady
  prijala ako preplatok). Natívne `CHECKOUT_NOT_FULLY_PAID` zostáva.
- Zmenu ceny v katalógu (staff) počas platby backend nevie zablokovať; zachytí ju
  kontrola pri dokončení a objednávka nevznikne.

## 9. Objednávka

Pri vytvorení objednávky backend uloží do **privátnych** metadát objednávky
podpísaný snapshot `maky.b2b.order.v1`: rozhodnutie, `taxReason`
(`EU_ICS_43` / `DOMESTIC_VAT` / `OUT_OF_SCOPE_EXPORT`), vstup (IČO, DIČ, IČ DPH),
firma, fakturačná a dodacia adresa, atestácia, použitý platobný snapshot, sumy.
Ukladá sa pre spravované checkouty a pre každý firemný nákup (aj zdanený).
