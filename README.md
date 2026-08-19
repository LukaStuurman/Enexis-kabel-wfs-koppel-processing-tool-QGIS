# Enexis kabel WFS ↔ CSV koppeling voor QGIS

QGIS Processing-plugin die Enexis WFS-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Automatische WFS-selectie

Je hoeft de WFS-laag niet meer zelf toe te voegen of te kiezen.

De Processing-tool gebruikt automatisch:

```text
https://opendata.enexis.nl/geoserver/wfs
```

Bij het starten vraagt de plugin `GetCapabilities` op en zoekt hij automatisch naar een featuretype waarvan de naam `e_lv_map_cable` bevat. Als er een featuretype bestaat waarvan de lokale naam exact `e_lv_map_cable` is, krijgt die voorrang.

Daarna:

1. wordt de gevonden laag automatisch als WFS-laag geladen;
2. wordt automatisch het veld `label` gezocht (hoofdletterongevoelig, met een fallback naar een veldnaam die `label` bevat);
3. worden alleen WFS-features opgevraagd waarvan het label voorkomt in de CSV, zodat niet onnodig de volledige Enexis-kabellaag wordt verwerkt;
4. wordt de eigenlijke 1-op-1 koppeling uitgevoerd.

De enige normale invoer die je in de Processing-tool hoeft te kiezen is dus de **CSV**.

## Koppelregels

1. Het WFS-label wordt genormaliseerd door alleen de bekende prefix `Kabelgroup: ` te verwijderen en witruimte aan begin/einde te trimmen.
2. Daarna moet het label **exact** overeenkomen met de CSV-kolom `Kabel Subgroep`.
3. De geometrische lengte van iedere WFS-lijn wordt in meters bepaald en op **2 decimalen** afgerond.
4. `Lengte [kaart] (m)` uit de CSV accepteert onder andere `195`, `16,5`, `196,11` en puntdecimalen.
5. Als dezelfde `Kabel Subgroep` meerdere keren voorkomt, wordt binnen uitsluitend die exacte labelgroep een globale 1-op-1 lengtematching uitgevoerd. Iedere WFS-lijn en iedere CSV-rij kan maximaal één keer worden gebruikt.
6. Bij ongelijke aantallen worden zo veel mogelijk paren gemaakt. Overtollige WFS-lijnen en CSV-rijen blijven expliciet ongekoppeld.

De 1-op-1 matching minimaliseert per labelgroep de totale absolute afwijking tussen WFS-lengtes en CSV-lengtes.

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
6. Kies alleen je CSV en start de tool.

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

Een tweede tabel bevat alle CSV-rijen die niet zijn gebruikt, inclusief reden.

## Praktische controle

Filter na het draaien op `match_status`. Controleer bij `GEKOPPELD` vooral `len_diff_m`. Er is bewust geen maximale lengte-afwijking ingebouwd: binnen een exact overeenkomende kabelsubgroep wordt altijd de best passende 1-op-1 combinatie gekozen.
