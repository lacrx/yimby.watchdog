# Municipal Obstruction Patterns

A taxonomy of tactics cities use to violate state housing law while maintaining plausible deniability. Each pattern includes the mechanism, how to detect it, the legal vulnerability it creates, and how to break it.

## How to Use This File

When analyzing a city's housing policy, compare its actions against these patterns. If you find a match:

1. Name the pattern explicitly
2. Cite the specific mechanism being used
3. Identify the legal vulnerability from [ca-housing-law.md](ca-housing-law.md)
4. Reference any case law from [case-law.md](case-law.md) where courts have rejected similar tactics
5. Estimate exposure using the penalty calculator

Most cities use 2-4 of these simultaneously. The patterns compound — a city using tier manipulation AND walking path exclusion AND Coastal Act shielding faces overlapping legal exposure from multiple statutes.

## The Patterns

### 1. Weaponized Neglect

**Mechanism:** City doesn't build sidewalks in an area → claims no "walking path" exists to a transit stop → excludes parcels from SB 79 transit housing mandate.

**Detection:**
- Compare city's sidewalk network with pedestrian usage data (people walk there even without sidewalks)
- Check state definition of "walking distance" — it's distance-based, not path-quality-based
- Cross-reference with the city's own capital improvement plans — if they've planned sidewalks, they know pedestrians walk there

**Legal vulnerability:** Infrastructure neglect is not a basis for exemption from state law. HCD defines walking distance by measurement, not by sidewalk quality. The city's choice not to build infrastructure cannot shield it from mandates that apply based on proximity.

**Breaks when:** HCD rejects the implementing ordinance; private litigants cite actual pedestrian usage; court finds the exclusion pretextual.

### 2. Tier Manipulation

**Mechanism:** City undercounts transit service at a station to classify it as a lower SB 79 tier, reducing required density allowances.

**Detection:**
- Count trains independently from each operator (Amtrak, Metrolink, Coaster, SPRINTER — they don't coordinate responses)
- Compare city's count against published GTFS schedules
- Check HCD March 2026 advisory: ALL trains from ALL operators must be aggregated across both directions

**Legal vulnerability:** SANDAG TOD map carries rebuttable presumption (§ 65912.160(f)). A city arguing against its own MPO's classification faces uphill battle. After Jan 1, 2027, denying SB 79 projects in high-resource areas triggers presumption of HAA violation.

**Red flag:** Any station with 130+ daily trains classified as Tier 2. The Tier 1 threshold is 72.

### 3. Coastal Act Shield

**Mechanism:** City routes density limits through CCC process (Local Coastal Program amendments) to override state density bonus law and housing mandates. Uses "coastal resources" as justification for blocking residential density near transit.

**Detection:**
- Check whether the city initiated LCP amendments that reduce density after state mandates increased it
- Look for CCC staff reports that acknowledge tension between coastal preservation and housing production
- Compare density allowed in coastal zone vs. inland areas for similar parcels

**Legal vulnerability:** The shield is weakening. New Commune v. Redondo Beach opened the door for BR in coastal zones. SB 79 likely prevails over Coastal Act (75-85% probability) based on statutory structure. CCC's own analyses increasingly acknowledge housing production obligations.

**Breaks when:** Court resolves the Coastal Act / housing law tension in favor of housing mandates; CCC declines to certify an LCP amendment designed to block state-mandated density; legislature explicitly subordinates Coastal Act to SB 79.

### 4. Creative Definitions

**Mechanism:** City redefines statutory terms narrowly to exclude parcels or projects from state mandates.

**Examples:**
- Classifying Good Cause eviction protection (AB 1482) as "rent control" to trigger SB 79 exemption → Good Cause ≠ rent stabilization under Costa-Hawkins
- Defining "walking path" as "continuous paved sidewalk" rather than the statutory walking distance → no statutory basis for path quality requirement
- Defining "dedicated bus lane" as requiring physical separation → SB 79 conspicuously omits physical exclusivity for bus lanes while requiring it for heavy rail

**Detection:**
- Compare city's definitions against statutory language
- Check HCD guidance documents
- Look for legislative intent in committee analyses
- Apply *expressio unius est exclusio alterius* — if the statute defines the term one way for one mode and differently for another, the difference is intentional

**Legal vulnerability:** HAA penalties for project denials based on fabricated definitions. HCD can reject implementing ordinances that narrow statutory terms. Courts apply plain statutory language, not city-invented glossaries.

### 5. Design Review as Delay

**Mechanism:** City uses subjective design standards to impose repeated continuances on housing projects, running out the developer's financing timeline without ever formally denying the project.

**Detection:**
- Count hearings per project. SB 330: max 5 hearings on compliant projects
- Check for "continued to date uncertain" entries in planning commission minutes
- Look for design review comments that are subjective ("doesn't fit the neighborhood") rather than tied to objective, pre-existing standards

**Legal vulnerability:** SB 330 hearing cap. AB 1893: unnecessary delay = disapproval, triggering HAA penalties. Cal. Renters v. San Mateo: subjective design standards insufficient basis for denial.

### 6. Post-Hoc Findings

**Mechanism:** City makes a political decision to deny a project, then generates objective-sounding denial findings after the fact.

**Detection:**
- Compare timeline: were findings drafted before or after the vote?
- Check if findings cite standards that didn't exist when the project was submitted
- Look for findings that mirror public comment language rather than code language

**Legal vulnerability:** HAA requires findings based on objective, pre-existing standards. Post-hoc rationalization is precisely what HAA § 65589.5(j) prohibits. CaRLA v. Huntington Beach rejected manufactured safety/traffic findings as pretextual.

### 7. Self-Certified Compliance

**Mechanism:** City declares its own housing element compliant without HCD certification, then uses that declaration to deny Builder's Remedy applications or claim exemption from enforcement.

**Detection:**
- Check HCD's housing element status dashboard
- Verify whether HCD has issued a certification letter
- Look for city staff reports that say "we have determined" compliance rather than "HCD has certified"

**Legal vulnerability:** Martinez v. Clovis: only HCD or a court determines substantial compliance. City self-certification is legally meaningless.

### 8. Maximize Exemptions

**Mechanism:** City drafts SB 79 phasing ordinance to exempt maximum parcels from transit-oriented density requirements — using habitat designations, historic districts, parking overlays, or other local designations to carve out areas.

**Detection:**
- Map exempted parcels vs. total parcels within ½ mile of TOD stops
- Check whether exemption categories existed before SB 79 or were created after
- Compare exempted acreage to the city's stated SB 79 compliance plan

**Legal vulnerability:** HCD must approve implementing ordinances. Exemptions exceeding statutory basis face litigation. Alternative plans must maintain same total zoned capacity as SB 79 default.

### 9. Revenue Misdirection

**Mechanism:** City claims new revenue from development "reinvested in community" while it actually flows to General Fund first, then gets allocated to suburban infrastructure maintenance.

**Detection:**
- Follow the money through budget line items
- Compare "community benefit" claims against actual budget allocations
- Check IBA or auditor reports on revenue disposition

**Legal vulnerability:** Not directly a housing law violation, but useful for Step 4 of the methodology (evaluating actions against stated goals). Exposes the gap between rhetoric and fiscal reality.

### 10. The Ponzi Extension

**Mechanism:** City approves new greenfield subdivision to generate impact fees and property tax revenue that funds maintenance of aging infrastructure in earlier subdivisions.

**Detection:**
- Compare infrastructure age in existing subdivisions against replacement schedule
- Calculate whether impact fees from new development cover lifecycle costs or just fund deferred maintenance elsewhere
- Check whether the city has an infrastructure replacement fund or relies on continuous new development

**Legal vulnerability:** Not a housing law violation per se, but the foundational fiscal dynamic that makes suburban development patterns unsustainable. Use this pattern to rebut "new development pays for itself" claims. See [evidence-base.md](evidence-base.md) for the lifecycle cost literature.

## Pattern Combinations

Cities rarely use just one. Common stacks:

| Combination | Effect |
|-------------|--------|
| Tier manipulation + walking path exclusion | Reduces both the density allowed AND the parcels it applies to |
| Coastal Act shield + creative definitions | Uses two independent legal theories to block the same projects |
| Design review delay + post-hoc findings | Delays project until financing expires, then manufactures denial rationale |
| Self-certified compliance + maximize exemptions | Claims compliance while exempting most eligible parcels |
| Revenue misdirection + Ponzi extension | Funds sprawl maintenance with infill revenue while claiming fiscal prudence |

When you find one pattern, look for others. The legal exposure from stacked patterns is multiplicative, not additive — each pattern creates independent causes of action.
