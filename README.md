# Enexis kabel WFS ↔ CSV koppeling voor QGIS 4.2

QGIS Processing-plugin die Enexis `e_lv_map_cable`-kabellijnen **strikt 1-op-1** koppelt aan rijen uit een CSV-export en gekoppelde kabels naar DXF kan exporteren.

## v0.14.0: snelle landelijke koppeling met twee permanente indexes

Versie 0.14 gebruikt voor heel Nederland niet meer bij iedere run opnieuw de volledige Enexis WFS-download. De plugin onderhoudt nu twee herbruikbare SQLite-indexes op lokale schijf:

- **CSV-index**: `Kabel Subgroep`, kaartlengte, CSV-rijnummer en oorspronkelijke CSV-waarden;
- **WFS-index**: stabiele bron-ID/geometry-hash, genormaliseerd kabelgroeplabel, RD-lengte en WKB-geometrie.

Na de eerste indexbouw is een volgende landelijke koppeling in principe volledig lokaal:

**CSV-index + WFS-index → lokale 1-op-1 matching → GeoPackage-output**

De netwerkverbinding met Enexis is dan niet meer de bottleneck, tenzij **Vernieuw landelijke WFS-index vanaf Enexis** wordt aangevinkt.

## De vijf landelijke snelheidsverbeteringen

### 1. WFS-download per RD-tegel

De eerste WFS-index wordt niet meer als één landelijke `startIndex`-reeks opgehaald. Nederland wordt verdeeld in RD-hoofdtegels van **25 × 25 km**. Iedere tegel gebruikt een WFS `bbox`.

Een hoofdtegel vraagt maximaal 5.001 objecten. Komt er meer terug of wordt de response te groot, dan wordt uitsluitend die tegel automatisch in vier kleinere BBOX-tegels verdeeld. Dit gaat adaptief door tot de response veilig past. De actieve landelijke route heeft daardoor geen `startIndex`- of `sortBy=fid`-paginering nodig.

Objecten op tegelgrenzen worden via hun WFS-feature-ID of, wanneer die niet beschikbaar is, een stabiele label+geometry-hash gededupliceerd. Onder ongeveer 1 km tegelgrootte stopt de plugin gecontroleerd wanneer een tegel nog steeds boven de veiligheidsgrens zit, zodat WFS-truncatie nooit stil wordt geaccepteerd.

### 2. Permanente WFS-index

De landelijke WFS-geometrieën blijven na een succesvolle indexbouw lokaal beschikbaar. De index bevat alleen de gegevens die voor koppeling nodig zijn en krijgt een index op het genormaliseerde label.

De WFS-index wordt opnieuw opgebouwd wanneer:

- er nog geen geldige WFS-index bestaat;
- de interne indexversie/schema niet meer overeenkomt;
- featuretype, labelveld, geometrieveld of CRS verandert;
- **Vernieuw landelijke WFS-index vanaf Enexis** expliciet wordt aangevinkt.

Een mislukte of geannuleerde vernieuwing wordt eerst in een apart bouwbestand uitgevoerd. De bestaande goede WFS-index blijft daardoor behouden totdat de nieuwe index volledig gereed is.

### 3. Geen kopie van de volledige CSV-index meer

v0.13 maakte voor een landelijke run eerst een volledige werkkopie van de CSV-index. Bij een index van honderden MB's kost dat onnodige SSD-I/O en extra vrije schijfruimte.

v0.14 opent de bestaande CSV-index en koppelt de WFS-index met SQLite `ATTACH DATABASE`. Alleen de gematchte CSV-rijnummers worden in een tijdelijke SQLite-tabel bijgehouden. `temp_store=FILE` houdt ook die runstatus schijfgebaseerd.

De permanente CSV- en WFS-index worden niet met `matched`-status vervuild.

### 4. Alleen GEKOPPELD uitvoeren

Voor landelijke runs staat standaard aan:

**Landelijk: alleen GEKOPPELD schrijven (snelste; geen unmatched CSV)**

In deze modus:

- worden alleen daadwerkelijk gekoppelde WFS-kabels naar de grote lijnoutput geschreven;
- worden overgebleven WFS-objecten niet uitgeschreven;
- wordt de landelijke unmatched-CSV-output niet gevuld.

Dit voorkomt miljoenen onnodige outputwrites. Schakel de optie uit wanneer een volledige analyse van niet-gekoppelde CSV-rijen nodig is.

### 5. Begrensde parallelisatie

De WFS-indexbouw gebruikt maximaal **2 gelijktijdige tegelrequests**. Dit versnelt netwerk-I/O zonder terug te gaan naar agressieve parallelisatie.

Ruwe tegelresponses worden naar tijdelijke bestanden geschreven. QGIS-geometrieparsing en SQLite-inserts gebeuren gecontroleerd buiten de netwerkthreads, zodat grote geometrysets niet tegelijk als Python/QGIS-objecten in RAM staan.

## GeoPackage-output rechtstreeks van Enexis WFS

GeoServer kan WFS GeoPackage-output aanbieden wanneer de GeoPackage Output Extension op de server is geïnstalleerd. v0.14 controleert dit tijdens een WFS-indexbouw met een echte minimale `outputFormat=geopkg`-request.

**Live controle op 21 augustus 2026:** Enexis GetCapabilities rapporteerde het kabeltype als `Enexis_Opendata:asm_e_lv_map_cable`. Een WFS `GetFeature` met `outputFormat=geopkg` gaf HTTP 400. De huidige Enexis WFS biedt dus geen directe GeoPackage-output aan en v0.14 gebruikt momenteel GeoJSON voor de tegelindex.

De probe blijft in de plugin aanwezig. Als Enexis de GeoPackage-extensie later inschakelt, wordt dit automatisch ontdekt. Als GeoPackage beschikbaar is, benchmark de plugin een kleine GeoJSON- en GeoPackage-response en kiest GeoPackage alleen wanneer de live meting een duidelijk voordeel in responstijd of overdrachtsgrootte laat zien.

## CSV-index

De CSV-index uit v0.13 blijft behouden. De CSV wordt alleen opnieuw geïndexeerd wanneer de bron is veranderd. De controle gebruikt onder andere:

- absoluut bestandspad;
- bestandsgrootte;
- wijzigingstijd;
- SHA-256-fingerprint van begin en einde van het bestand;
- interne indexversie.

Bij een kleine schermextent worden na de WFS-labelscan alleen relevante CSV-labels via SQLite `WHERE label IN (...)` geladen. De volledige landelijke CSV hoeft dus niet opnieuw te worden doorgelopen.

## Extentmodus

Kies bij **Beperk WFS tot scherm/gebied** bij voorkeur **Use current map canvas extent**.

De bestaande snelle extentroute blijft:

1. extent naar EPSG:28992;
2. alleen WFS-labelattribuut binnen de extent ophalen;
3. relevante CSV-rijen via de herbruikbare CSV-index selecteren;
4. alleen gezamenlijke labels als WFS-geometrie opvragen;
5. `BBOX(geografischeligging, ...) AND label IN (...)` gebruiken;
6. strikt 1-op-1 op kaartlengte matchen.

De labelscan is begrensd op 10.000 kabeldelen / 4 MB. Geometriebatches bevatten maximaal 10 labels, maximaal 1.000 features en maximaal 8 MB.

## Landelijke modus gebruiken

Laat **Beperk WFS tot scherm/gebied** leeg.

Aanbevolen instellingen:

- **CSV-index/cachemap:** vaste map op lokale SSD;
- **Vernieuw landelijke WFS-index:** alleen aan voor een bewuste data-update;
- **Alleen GEKOPPELD schrijven:** aan voor maximale snelheid;
- **Gekoppelde WFS-lijnen:** expliciet GeoPackage-bestand op lokale SSD;
- unmatched-output mag bij matched-only leeg/tijdelijk zijn; wanneer matched-only uitstaat moet ook deze grote output naar schijf.

De eerste landelijke v0.14-run kan nog lang duren omdat circa twee miljoen WFS-kabeldelen eenmalig lokaal moeten worden geïndexeerd. De grote winst zit vooral in alle volgende landelijke koppelingen: de WFS hoeft dan niet opnieuw te worden gedownload.

## Koppelregels

1. `Kabelgroup: WLR1760-03` wordt genormaliseerd naar `WLR1760-03`.
2. Het genormaliseerde WFS-label moet exact gelijk zijn aan CSV `Kabel Subgroep`.
3. WFS-lengte wordt in RD New in meters berekend en op twee decimalen afgerond.
4. Binnen hetzelfde label wordt strikt 1-op-1 gekoppeld op minimale totale absolute lengteafwijking.
5. Iedere WFS-lijn en CSV-rij wordt maximaal één keer gebruikt.
6. Er geldt geen maximale lengtetolerantie; `len_diff_m` laat de afwijking zien.

## CSV

Minimaal vereist:

- `Kabel Subgroep`
- `Lengte [kaart] (m)`

Komma- en puntdecimalen worden ondersteund.

## DXF

De plugin bevat ook **Split gekoppelde kabels naar DXF (V6 - landelijk)**. De landelijke DXF-modus streamt features rechtstreeks naar begrensde DXF-delen en verzamelt geen landelijke geometrieverzameling in RAM.

## QGIS-versie

Doelplatform: **QGIS 4.2.0 / Qt6**.

CI draait de pure Python-index/matchingtests, importeert de actieve provider in de officiële `qgis/qgis:4.2.0-questing` container met Qt offscreen en voert daarnaast een niet-blokkerende live Enexis WFS-formaatprobe uit.
