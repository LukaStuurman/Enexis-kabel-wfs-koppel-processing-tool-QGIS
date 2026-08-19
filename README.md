# Enexis kabel WFS ↔ CSV koppeling voor QGIS

QGIS Processing-plugin die Enexis WFS-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Automatische WFS-selectie

Je hoeft de WFS-laag niet zelf toe te voegen of te kiezen.

De Processing-tool gebruikt automatisch:

```text
https://opendata.enexis.nl/geoserver/wfs
```

Bij de eerste run in een QGIS-sessie vraagt de plugin `GetCapabilities` op en zoekt hij naar een featuretype waarvan de naam `e_lv_map_cable` bevat. Als er een featuretype bestaat waarvan de lokale naam exact `e_lv_map_cable` is, krijgt die voorrang. De gevonden naam wordt daarna voor de rest van de QGIS-sessie gecachet.

Daarna:

1. wordt de gevonden laag automatisch als WFS-laag geladen;
2. wordt automatisch het veld `label` gezocht;
3. worden alleen geldige `Kabel Subgroep`-labels uit de CSV in het WFS-filter opgenomen;
4. probeert de plugin dit filter direct als subset op de WFS-provider te zetten, zodat de filtering zo vroeg mogelijk bij de bron plaatsvindt;
5. kan de WFS-opvraag optioneel ook tot een scherm/gebied-extent worden beperkt;
6. wordt de eigenlijke 1-op-1 koppeling uitgevoerd.

## Alleen huidige schermextent gebruiken

Naast de CSV heeft de tool een **optionele extent-invoer**:

- **Extent leeg laten:** de tool beperkt de WFS alleen op de Kabel Subgroep-labels uit de CSV.
- **Use current map canvas extent:** alleen WFS-kabels die de huidige kaartweergave raken worden opgehaald en verwerkt.
- Je kunt via dezelfde standaard QGIS extent-selector ook een gebied tekenen, een laagextent kiezen of een bookmark/layout-extent gebruiken.

De gekozen extent wordt door QGIS automatisch omgerekend naar het CRS van de Enexis WFS-laag. Daarna wordt hij als ruimtelijke `QgsFeatureRequest`-filter (`setFilterRect`) toegepast, naast het label-filter.

Als een CSV-regel bij een actieve extent geen WFS-kabel binnen het gekozen gebied heeft, krijgt die in de tabel met niet-gekoppelde CSV-regels de reden `GEEN_MATCH_BINNEN_EXTENT`.

## Koppelregels

1. Het WFS-label wordt genormaliseerd door alleen de bekende prefix `Kabelgroup: ` te verwijderen en witruimte aan begin/einde te trimmen.
2. Daarna moet het label **exact** overeenkomen met de CSV-kolom `Kabel Subgroep`.
3. De geometrische lengte van iedere WFS-lijn wordt in meters bepaald en op **2 decimalen** afgerond.
4. `Lengte [kaart] (m)` uit de CSV accepteert onder andere `195`, `16,5`, `196,11` en puntdecimalen.
5. Als dezelfde `Kabel Subgroep` meerdere keren voorkomt, wordt binnen uitsluitend die exacte labelgroep een globale 1-op-1 lengtematching uitgevoerd. Iedere WFS-lijn en iedere CSV-rij kan maximaal één keer worden gebruikt.
6. Bij ongelijke aantallen worden zo veel mogelijk paren gemaakt. Overtollige WFS-lijnen en CSV-rijen blijven expliciet ongekoppeld.

De 1-op-1 matching minimaliseert per labelgroep de totale absolute afwijking tussen WFS-lengtes en CSV-lengtes.

## Prestatie-optimalisaties

De automatische variant is bewust geoptimaliseerd zonder onveilige extra QGIS-threads:

- **Server-side labelfiltering:** eerst wordt geprobeerd het label-filter direct op de WFS-provider te zetten. Hierdoor hoeft normaal niet de landelijke kabellaag naar QGIS te worden gehaald.
- **Optionele extentfilter:** bij een gekozen scherm/gebied wordt de feature-opvraag ook ruimtelijk beperkt, wat vooral bij grote labelsets netwerk- en verwerkingstijd kan besparen.
- **Alleen bruikbare labels:** CSV-regels zonder geldig label of zonder geldige lengte veroorzaken geen onnodige WFS-opvraag.
- **WFS-laagnaamcache:** `GetCapabilities` voor de automatische type-detectie wordt binnen dezelfde QGIS-sessie maar één keer gedaan.
- **Lengteberekening voorbereid:** CRS-conversie en `QgsDistanceArea` worden één keer voorbereid in plaats van opnieuw per feature.
- **Lichtere featureadministratie:** intern worden compacte tuples gebruikt in plaats van dictionaries per WFS-feature.
- **Snelle matching bij gelijke aantallen:** wanneer WFS en CSV binnen een kabelgroep evenveel delen hebben, is sorteren + positie-op-positie koppelen globaal optimaal. Dit is `O(n log n)` in plaats van de eerdere `O(n²)` dynamic programming.
- **Compactere matching bij ongelijke aantallen:** de optimale DP gebruikt nog maar twee kostenrijen en één byte per terugzoekbeslissing, in plaats van twee volledige Python-matrices.

### Waarom geen extra threads/cores?

QGIS Processing voert algoritmes standaard al in een aparte achtergrondthread uit. QGIS-objecten zoals `QgsVectorLayer` moeten niet zonder meer over extra worker-threads worden gedeeld. De CPU-matching is pure Python, waardoor gewone Python-threads door de GIL ook weinig tot geen versnelling geven.

Meerdere processen zouden alleen interessant worden bij uitzonderlijk grote kabelgroepen, maar hebben op Windows/QGIS relatief veel opstart- en serialisatie-overhead. Voor deze tool is het veel effectiever om de WFS-opvraag klein te houden en het matching-algoritme zelf efficiënter te maken.

## CSV-input

De CSV moet minimaal deze kolommen bevatten:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Het scheidingsteken wordt automatisch gedetecteerd. Komma- en puntdecimalen worden ondersteund.

## Installatie

1. Download deze repository als ZIP.
2. Open QGIS.
3. Ga naar **Plugins → Plugins beheren en installeren → Installeren vanuit ZIP**.
4. Kies de ZIP en installeer **Enexis Kabel WFS-CSV Koppeling**.
5. Open **Processing → Toolbox → Enexis → Kabelkoppeling → Koppel Enexis WFS-kabels automatisch aan CSV (1-op-1)**.
6. Kies je CSV.
7. Optioneel: kies bij de extent-invoer **Use current map canvas extent** als je alleen het huidige scherm wilt verwerken.
8. Start de tool.

Een internetverbinding is vereist tijdens het uitvoeren, omdat QGIS de Enexis WFS-service rechtstreeks benadert.

## Uitvoer

### Gekoppelde WFS-lijnen

Aan de gevonden WFS-lijnen worden toegevoegd:

- alle CSV-kolommen met prefix `csv_` wanneer er een match is;
- `match_status`;
- `wfs_label_norm`;
- `wfs_len_m`;
- `csv_len_m`;
- `len_diff_m`;
- `csv_row_nr`.

Belangrijke statussen zijn onder andere `GEKOPPELD`, `GEEN_EXACT_LABEL_IN_CSV`, `GEEN_CSV_RIJ_OVER_IN_LABELGROEP`, `LEGE_WFS_LABEL` en `ONGELDIGE_WFS_GEOMETRIE`.

### Niet-gekoppelde CSV-rijen

Een tweede tabel bevat alle CSV-rijen die niet zijn gebruikt, inclusief reden. Met een actieve extent wordt `GEEN_MATCH_BINNEN_EXTENT` gebruikt wanneer voor die CSV-regel geen passende WFS-kabel binnen het gekozen gebied is opgehaald.

## Praktische controle

Filter na het draaien op `match_status`. Controleer bij `GEKOPPELD` vooral `len_diff_m`. Er is bewust geen maximale lengte-afwijking ingebouwd: binnen een exact overeenkomende kabelsubgroep wordt altijd de best passende 1-op-1 combinatie gekozen.
