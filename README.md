# Enexis kabel WFS/SHAPE ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis laagspanningskabels **strikt 1-op-1** koppelt aan rijen uit een landelijke CSV en gekoppelde kabels naar DXF kan exporteren.

## v0.15.0: landelijke bronkeuze WFS of automatische SHAPE-download

Voor kleine kaartgebieden blijft de bestaande snelle WFS-extentroute actief. Voor landelijke verwerking kan v0.15 kiezen uit:

1. **WFS (online tegelindex)**
2. **SHAPE Noord (automatisch downloaden)**
3. **SHAPE Zuid (automatisch downloaden)**
4. **SHAPE Noord + Zuid (automatisch downloaden)** — standaard voor heel Nederland

De SHAPE-route is bedoeld als snellere en minder netwerkafhankelijke landelijke bron. Na de eerste download en indexbouw zijn volgende koppelingen volledig lokaal.

## Automatische Enexis SHAPE-download

De plugin bevat de door de gebruiker aangeleverde Enexis-downloadlink. De Outlook SafeLinks-wrapper is niet nodig in QGIS: de plugin gebruikt rechtstreeks het daarin opgenomen Spotler-doel en volgt automatisch de redirect naar het ZIP-bestand.

Bij de eerste SHAPE-run:

1. de ZIP wordt **streamend naar de lokale cachemap** gedownload; de volledige ZIP komt dus niet in RAM;
2. de tijdelijke download wordt als ZIP gevalideerd;
3. alleen deze map wordt uit het archief gehaald:
   `imkl_elektriciteitskabel_e_lv_map_cable_ligging`;
4. in die map moet exact één `.shp` met **Noord/North** en één `.shp` met **Zuid/South** in de bestandsnaam staan;
5. bij beide SHAPE-bestanden worden `.dbf`, `.shx` en `.prj` vereist;
6. de gekozen regio('s) worden naar een permanente lokale SQLite-kabelindex geconverteerd.

De extractie gebeurt eerst in een tijdelijke map. Pas nadat de ZIP-inhoud en beide SHAPE-sets geldig zijn, wordt de bestaande cache atomair vervangen. Mislukt dat vervangen, dan wordt de vorige geldige extractie teruggezet.

## SHAPE-cache vernieuwen

De ZIP wordt niet bij iedere run opnieuw gedownload. Dat zou de landelijke snelheidswinst tenietdoen.

De eerste run downloadt automatisch. Daarna worden zowel de ZIP als de permanente kabelindex hergebruikt. Voor een nieuwe Enexis-uitgave vink je aan:

**SHAPE ZIP opnieuw downloaden en SHAPE-index vernieuwen**

Een gewijzigde lokale ZIP wordt tevens herkend via bestandsgrootte, wijzigingstijd en een SHA-256-fingerprint van begin en einde van het bestand.

## Noord, Zuid of heel Nederland

- **SHAPE Noord** leest alleen de Noord-SHAPE uit de gedownloade ZIP.
- **SHAPE Zuid** leest alleen de Zuid-SHAPE.
- **SHAPE Noord + Zuid** leest beide en dedupliceert identieke grensobjecten op genormaliseerd label + geometrie.

Voor een landelijke run staat **Noord + Zuid standaard geselecteerd**.

## Kabelgroep uit SHAPE

De plugin detecteert automatisch het kabelgroep/labelveld in de DBF. Eerst worden duidelijke veldnamen gebruikt, zoals `label`, `kabelgroep`, `kabelgroup` of een door Shapefile afgekorte variant. Wanneer de veldnaam niet duidelijk genoeg is, worden voorbeeldwaarden gecontroleerd op bekende prefixes.

Voor SHAPE worden onder andere deze vormen genormaliseerd:

- `Kabelgroup: WLR1760-03` → `WLR1760-03`
- `Kabelgroep: WLR1760-03` → `WLR1760-03`
- `Cablegroup: WLR1760-03` → `WLR1760-03`

Daarna blijft de vergelijking met CSV `Kabel Subgroep` exact en case-sensitive.

## CRS en geometrie

De uiteindelijke kabelindex gebruikt **EPSG:28992 / RD New**. Als de SHAPE een ander geldig CRS bevat, transformeert QGIS de geometrie naar RD voordat de lengte wordt berekend.

LineString-geometrieën worden naar MultiLineString gepromoveerd zodat ze veilig naar dezelfde landelijke GeoPackage-output kunnen worden geschreven als de WFS-route.

## Permanente lokale indexes

De landelijke workflow gebruikt twee lokale indexes:

- **CSV-index**: CSV-rijnummer, `Kabel Subgroep`, kaartlengte en oorspronkelijke CSV-waarden;
- **kabelindex**: genormaliseerd kabelgroeplabel, RD-lengte en WKB-geometrie uit SHAPE of WFS.

De CSV-index wordt niet meer naar een volledige werkkopie gekopieerd. SQLite koppelt de kabelindex met `ATTACH DATABASE`; alleen de gematchte CSV-rijnummers staan tijdelijk op schijf.

Na de eerste SHAPE-download/indexbouw is de normale route dus:

**CSV-index + SHAPE-kabelindex → lokale 1-op-1 matching → GeoPackage-output**

Er zijn dan geen landelijke WFS-requests nodig.

## WFS blijft beschikbaar

De bestaande v0.14 WFS-route blijft als fallback beschikbaar. Die gebruikt:

- RD-hoofdtegels van 25 × 25 km;
- adaptieve ruimtelijke opsplitsing voor te drukke tegels;
- maximaal twee gelijktijdige netwerkrequests;
- geen landelijke hoge `startIndex`-paginering;
- een permanente lokale WFS-kabelindex.

De WFS-server bood bij de live controle van 21 augustus 2026 geen directe GeoPackage-output: `outputFormat=geopkg` gaf HTTP 400. Daarom gebruikt de WFS-fallback momenteel GeoJSON wanneer een WFS-index opnieuw moet worden opgebouwd.

## Snelle landelijke output

Voor landelijke runs staat standaard aan:

**Landelijk: alleen GEKOPPELD schrijven (snelste; geen unmatched CSV)**

Hierdoor worden alleen daadwerkelijk gekoppelde kabels naar de grote lijnoutput geschreven. Zet de optie uit wanneer ook alle niet-gekoppelde CSV-rijen nodig zijn.

Gebruik voor de landelijke output expliciet een **GeoPackage op lokale SSD**. Grote landelijke memory-/temporary-uitvoer wordt waar nodig geweigerd om RAM-problemen te voorkomen.

## Extentmodus

Wanneer **Beperk WFS tot scherm/gebied** is ingevuld, wordt de landelijke bronkeuze genegeerd en gebruikt de plugin de bestaande lichte WFS-route:

1. alleen labels binnen de extent ophalen;
2. alleen relevante labels uit de herbruikbare CSV-index lezen;
3. alleen gezamenlijke labels als geometrie opvragen;
4. `BBOX(...) AND label IN (...)` toepassen;
5. strikt 1-op-1 op kaartlengte matchen.

De labelscan is begrensd op 10.000 kabeldelen / 4 MB. Geometriebatches bevatten maximaal 10 gezamenlijke labels, maximaal 1.000 features en maximaal 8 MB.

## Koppelregels

1. WFS/SHAPE-kabelgroep wordt naar de kale kabelgroepsleutel genormaliseerd.
2. De sleutel moet exact gelijk zijn aan CSV `Kabel Subgroep`.
3. Geometrielengte wordt in RD New in meters berekend en op twee decimalen afgerond.
4. Binnen hetzelfde label wordt strikt 1-op-1 gekoppeld op minimale totale absolute lengteafwijking.
5. Iedere kabelgeometrie en CSV-rij wordt maximaal één keer gebruikt.
6. Er geldt geen maximale lengtetolerantie; `len_diff_m` laat de afwijking zien.

## CSV

Minimaal vereist:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Komma- en puntdecimalen worden ondersteund.

## DXF

De plugin bevat ook **Split gekoppelde kabels naar DXF (V6 - landelijk)**. De landelijke DXF-modus streamt features rechtstreeks naar begrensde DXF-delen en verzamelt geen landelijke geometrieverzameling in RAM.

## QGIS-versie en tests

Doelplatform: **QGIS 4.2.0 / Qt6**.

CI test onder andere:

- de strikte matching;
- herbruikbare CSV- en kabelindexes;
- veilige ZIP-extractie en Noord/Zuid-detectie met een synthetische ZIP;
- gesimuleerde streamende ZIP-download zonder de echte grote Enexis-ZIP op te halen;
- de actieve provider en alle Processing-parameters in de officiële `qgis/qgis:4.2.0-questing` container;
- SHAPE-labelnormalisatie en LineString→MultiLineString-conversie in echte QGIS 4.2;
- de bestaande WFS-tegelroute en niet-blokkerende live WFS-formaatprobe.
