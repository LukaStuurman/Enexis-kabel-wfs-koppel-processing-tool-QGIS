# Enexis kabel WFS ↔ CSV koppeling voor QGIS

QGIS Processing-plugin die Enexis WFS-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Koppelregels

1. Het gekozen WFS-labelveld wordt genormaliseerd door alleen de bekende prefix `Kabelgroup: ` te verwijderen en witruimte aan begin/einde te trimmen.
2. Daarna moet het label **exact** overeenkomen met de CSV-kolom `Kabel Subgroep`. Er wordt dus niet fuzzy op kabelsubgroep gematcht en hoofdletters/tekens worden niet aangepast.
3. De geometrische lengte van iedere WFS-lijn wordt in meters bepaald en op **2 decimalen** afgerond.
4. `Lengte [kaart] (m)` uit de CSV accepteert o.a. `195`, `16,5`, `196,11` en puntdecimalen.
5. Als dezelfde `Kabel Subgroep` meerdere keren voorkomt, wordt binnen uitsluitend die exacte labelgroep een globale 1-op-1 lengtematching uitgevoerd. Iedere WFS-lijn en iedere CSV-rij kan maximaal één keer worden gebruikt.
6. Bij ongelijke aantallen worden zo veel mogelijk paren gemaakt (de volledige kleinste kant). Overtollige WFS-lijnen en CSV-rijen blijven expliciet ongekoppeld.

De 1-op-1 matching minimaliseert per labelgroep de totale absolute afwijking tussen WFS-lengtes en CSV-lengtes. Daardoor kan een vroege lokale keuze niet dezelfde CSV-rij "afpakken" van een betere combinatie verderop.

## Input

- Een **lijnlaag** in QGIS. Dit mag rechtstreeks een Enexis WFS-laag zijn.
- Het veld met het WFS-label, standaard voorgesteld als `label`.
- Een CSV met minimaal:
  - `Kabel Subgroep`
  - `Lengte [kaart] (m)`

De voorbeeld-CSV waarvoor dit is gebouwd gebruikt `;` als scheidingsteken en een komma als decimaalteken; de tool detecteert het scheidingsteken automatisch.

## Enexis WFS laden

Gebruik in QGIS **Gegevensbronnen beheren → WFS / OGC API Features** en voeg de Enexis WFS-service toe:

```text
https://opendata.enexis.nl/geoserver/wfs?request=getcapabilities
```

Kies vervolgens de relevante kabellijnlaag. De Processing-tool werkt op de gekozen lijnlaag en is dus niet afhankelijk van een hardgecodeerde WFS-laagnaam.

## Installatie

1. Download deze repository als ZIP.
2. Open QGIS.
3. Ga naar **Plugins → Plugins beheren en installeren → Installeren vanuit ZIP**.
4. Kies de ZIP en installeer **Enexis Kabel WFS-CSV Koppeling**.
5. Open **Processing → Toolbox → Enexis → Kabelkoppeling → Koppel WFS-kabels aan CSV (1-op-1)**.

## Uitvoer

### Gekoppelde WFS-lijnen

Alle invoerlijnen blijven behouden. Aan iedere lijn worden toegevoegd:

- alle CSV-kolommen met prefix `csv_` wanneer er een match is;
- `match_status`;
- `wfs_label_norm`;
- `wfs_len_m`;
- `csv_len_m`;
- `len_diff_m`;
- `csv_row_nr`.

Mogelijke belangrijke statussen zijn `GEKOPPELD`, `GEEN_EXACT_LABEL_IN_CSV`, `GEEN_CSV_RIJ_OVER_IN_LABELGROEP`, `LEGE_WFS_LABEL` en `ONGELDIGE_WFS_GEOMETRIE`.

### Niet-gekoppelde CSV-rijen

Een tweede tabel bevat alle CSV-rijen die niet zijn gebruikt, inclusief reden. Zo kun je direct controleren of er bijvoorbeeld meer CSV-delen dan WFS-lijnen in een kabelsubgroep staan.

## Praktische controle

Sorteer of filter na het draaien op `match_status` en controleer bij `GEKOPPELD` vooral `len_diff_m`. Er is bewust **geen maximale lengte-afwijking** ingebouwd: volgens de koppelregel wordt binnen een exact overeenkomende kabelsubgroep altijd de best passende 1-op-1 combinatie gekozen. Een opvallend grote `len_diff_m` blijft daardoor zichtbaar voor handmatige QA.
