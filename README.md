# Enexis kabel WFS ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis `e_lv_map_cable`-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export.

## Vereiste versie

Vanaf **v0.5.0** is de plugin expliciet gebouwd voor **QGIS 4.2.0 of nieuwer binnen de 4.x-reeks**. De code gebruikt de Qt6/PyQt6-compatibele QGIS 4 API:

- `QMetaType.Type.*` voor veldtypes in plaats van oude `QVariant.*`-types;
- `Qgis.WkbType.*` voor geometrie-enums;
- `QgsFeatureSink.Flag.FastInsert` voor sink-flags;
- `Qgis.ProcessingFileParameterBehavior.File` voor Processing-bestandsparameters.

De pluginmetadata heeft daarom `qgisMinimumVersion=4.2`.

## Geen live QGIS WFS-provider

Versie 0.3.0 gebruikte een live `QgsVectorLayer` met de QGIS WFS-provider, een attributenfilter en een BBOX-filter. Dat bleek in de praktijk traag en kon bij de geteste schermextent QGIS laten crashen.

Vanaf **v0.4.0** en dus ook in v0.5.0 is die route verwijderd. De plugin:

1. bouwt zelf een WFS `GetFeature`-request naar `https://opendata.enexis.nl/geoserver/wfs`;
2. stuurt de `Kabel Subgroep`-labels direct als GeoServer `cql_filter` mee;
3. stuurt een gekozen scherm/gebied-extent direct als WFS `bbox` mee;
4. vraagt het resultaat direct als `application/json` (GeoJSON) op;
5. verwerkt pas daarna de GeoJSON-features in QGIS.

Er wordt dus **geen live WFS-laag meer aangemaakt**, geen `setSubsetString()` uitgevoerd en de QGIS WFS achtergrondcache/downloader wordt niet gebruikt.

## Invoer

De tool vraagt alleen:

- **CSV-bestand**
- **Beperk WFS tot scherm/gebied** (optioneel)

De CSV moet minimaal bevatten:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Het CSV-scheidingsteken wordt automatisch gedetecteerd. Lengtes als `195`, `16,5`, `196,11` en `196.11` worden ondersteund.

## Schermextent

Bij de extent-invoer kun je in QGIS **Use current map canvas extent** kiezen.

- Extent leeg: alleen filter op Kabel Subgroep.
- Extent gekozen: Kabel Subgroep **én** BBOX worden al naar de WFS-server gestuurd.
- De extent wordt door QGIS naar **EPSG:28992 (RD New)** omgerekend voordat de WFS-oproep wordt gemaakt.
- De lijn wordt niet op de schermrand afgeknipt; de volledige WFS-geometrie wordt gebruikt voor de lengte.

CSV-rijen zonder gevonden kabel binnen een actieve extent krijgen `GEEN_MATCH_BINNEN_EXTENT`.

## Snellere WFS-opvraging

### Permanente laagnaamcache

De gevonden volledige WFS-typename wordt in de QGIS 4-instellingen opgeslagen. Daardoor hoeft na een QGIS-herstart niet opnieuw steeds `GetCapabilities` te worden doorlopen.

Als nog geen typename bekend is probeert de plugin eerst rechtstreeks `e_lv_map_cable`. Alleen wanneer GeoServer die ongekwalificeerde naam niet accepteert, wordt eenmalig `GetCapabilities` gebruikt om de volledige namespace/typename te vinden.

### Parallelle HTTP-oproepen

Bij grotere CSV's worden labels in batches van maximaal 40 kabelgroepen verdeeld. Als meerdere batches nodig zijn, gebruikt de plugin maximaal **4 HTTP-threads**.

Deze threads doen uitsluitend netwerk-I/O en raken geen QGIS-laag, geometrie of projectobject aan.

Bij kleine CSV's wordt bewust maar één request gebruikt, omdat extra parallelle requests dan meestal geen winst geven.

## Koppelregels

1. WFS-label `Kabelgroup: WLR1760-03` wordt genormaliseerd naar `WLR1760-03`.
2. Dit moet daarna **exact** gelijk zijn aan CSV `Kabel Subgroep`.
3. WFS-geometrie wordt in RD New als kaartlengte in meters gemeten en op 2 decimalen afgerond.
4. Binnen dezelfde Kabel Subgroep wordt strikt 1-op-1 gematcht op minimale lengte-afwijking.
5. Iedere WFS-lijn en iedere CSV-rij wordt maximaal één keer gebruikt.
6. Bij gelijke aantallen wordt de optimale matching via gesorteerde lengtes in `O(n log n)` uitgevoerd.
7. Bij ongelijke aantallen blijft de globale optimale 1-op-1 dynamic-programming matching behouden met beperkt geheugengebruik.

## Uitvoer

### Gekoppelde WFS-lijnen

De output bevat de WFS-geometrieën en attributen, plus:

- alle CSV-kolommen met prefix `csv_` bij een match;
- `match_status`;
- `wfs_label_norm`;
- `wfs_len_m`;
- `csv_len_m`;
- `len_diff_m`;
- `csv_row_nr`.

### Niet-gekoppelde CSV-rijen

Een tweede tabel bevat alle niet gebruikte CSV-regels met een reden, bijvoorbeeld:

- `GEEN_MATCH_BINNEN_EXTENT`
- `GEEN_EXACT_LABEL_IN_WFS`
- `GEEN_WFS_LIJN_OVER_IN_LABELGROEP`
- `ONGELDIGE_CSV_LENGTE`

## Installatie in QGIS 4.2.0

1. Download de nieuwste repository als ZIP.
2. Open **QGIS 4.2.0**.
3. Ga naar **Plugins → Plugins beheren en installeren → Installeren vanuit ZIP**.
4. Installeer **Enexis Kabel WFS-CSV Koppeling**.
5. Controleer dat versie **0.5.0 of hoger** actief is.
6. Open **Processing → Toolbox → Enexis → Kabelkoppeling → Koppel Enexis WFS-kabels automatisch aan CSV (1-op-1)**.
7. Kies je CSV en eventueel **Use current map canvas extent**.

Gebruik versie 0.3.0 niet meer voor deze workflow.
