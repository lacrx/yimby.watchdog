#!/usr/bin/env python3
"""Generate Word document: Oceanside 2026 Housing Pipeline Collapse."""

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

doc = Document()

style = doc.styles['Normal']
font = style.font
font.name = 'Calibri'
font.size = Pt(11)
font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)

for level in range(1, 4):
    hs = doc.styles[f'Heading {level}']
    hs.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
    hs.font.name = 'Calibri'

# ─── Title ───
title = doc.add_heading('Oceanside Housing Pipeline Collapse\nJanuary–June 2026', level=1)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER

subtitle = doc.add_paragraph()
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = subtitle.add_run('Data Briefing — June 2026')
run.font.size = Pt(12)
run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
run.font.italic = True

doc.add_paragraph()

# ─── Section 1: The Number ───
doc.add_heading('One Housing Application in Six Months', level=2)

p = doc.add_paragraph()
run = p.add_run(
    'Between January 1 and June 19, 2026, the City of Oceanside received one '
    'discretionary housing application.'
)
run.bold = True

doc.add_paragraph(
    'That application — DB26-00001, a 142-unit multifamily project at 1640 Oceanside '
    'Boulevard — was filed under AB 2011 (the Affordable Housing and High Road Jobs Act), '
    'a state law that converts the approval to a ministerial process. The developer used '
    'state law to bypass city discretionary review entirely. Without AB 2011, the count '
    'would be zero.'
)

doc.add_paragraph(
    'This is a city of 183,000 people with a 6th cycle RHNA obligation of approximately '
    '7,100 units by 2029.'
)

# ─── Section 2: Historical Context ───
doc.add_heading('Historical Comparison: Discretionary Housing Filings', level=2)

doc.add_paragraph(
    'The following table shows all discretionary development applications (D-, DB-, and '
    'RD-prefix projects in the city\'s eTRAKiT permit system) filed per year, alongside '
    'total housing units reported to HCD via the Annual Progress Report.'
)

table1 = doc.add_table(rows=10, cols=4, style='Light Shading Accent 1')
table1.alignment = WD_TABLE_ALIGNMENT.CENTER
headers1 = ['Year', 'Discretionary Apps\n(D/DB/RD)', 'Total Units Filed\n(HCD APR)', 'Large Projects\n(50+ units)']
data1 = [
    ['2018', '22', '576', '1'],
    ['2019', '24', '1,090', '3'],
    ['2020', '23', '399', '1'],
    ['2021', '24', '1,698', '8'],
    ['2022', '36', '3,211', '13'],
    ['2023', '35', '1,977', '7'],
    ['2024', '34', '2,023', '10'],
    ['2025', '25 (18 housing)', '1,954', '7'],
    ['2026 (Jan–Jun)', '5 (1 housing)', '—', '0'],
]
for i, h in enumerate(headers1):
    cell = table1.rows[0].cells[i]
    cell.text = h
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True
            r.font.size = Pt(9)
for ri, row_data in enumerate(data1):
    for ci, val in enumerate(row_data):
        cell = table1.rows[ri + 1].cells[ci]
        cell.text = val
        if ri == 8:
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
                    r.font.color.rgb = RGBColor(0x8B, 0x00, 0x00)

doc.add_paragraph()

doc.add_paragraph(
    'In 2025, the city received 18 discretionary housing applications. In the first half '
    'of 2026, it received one. The remaining 4 discretionary applications in 2026 were a '
    'police training facility, a medical office, and two Walmart expansions.'
)

# ─── Section 3: 2025 vs 2026 Full Comparison ───
doc.add_heading('2025 vs. 2026: Discretionary and Ministerial', level=2)

doc.add_paragraph(
    'The city\'s eTRAKiT system has two search portals: a project search for discretionary '
    'applications and a permit search for ministerial building permits. The following table '
    'combines both to show total housing activity.'
)

table2 = doc.add_table(rows=9, cols=3, style='Light Shading Accent 1')
table2.alignment = WD_TABLE_ALIGNMENT.CENTER
headers2 = ['', '2025\n(full year)', '2026\n(Jan–Jun)']
data2 = [
    ['DISCRETIONARY (project search)', '', ''],
    ['  Housing applications', '18', '1'],
    ['', '', ''],
    ['MINISTERIAL (building permits)', '', ''],
    ['  ADUs', '156 units', '66 units'],
    ['  Single Family', '285 units', '60 units'],
    ['  Duplex', '46 units', '2 units'],
    ['  Multi Family + Mid/High Rise', '243 units (est.)', '205 units (est.)'],
]
for i, h in enumerate(headers2):
    cell = table2.rows[0].cells[i]
    cell.text = h
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True
for ri, row_data in enumerate(data2):
    for ci, val in enumerate(row_data):
        cell = table2.rows[ri + 1].cells[ci]
        cell.text = val
        if row_data[0].startswith('DISC') or row_data[0].startswith('MINI'):
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
                    r.font.size = Pt(10)

doc.add_paragraph()

doc.add_paragraph(
    'Notes on the 2026 ministerial numbers:'
)

notes = [
    '57 of 60 single-family permits are North River Farms tract lots — a pre-entitled '
    'subdivision, not new development capacity.',
    '199 of 205 multi-family/mid-rise units are Olive Park Affordable Apartments '
    '(DB24-00001, approved 2024) — a single 100% affordable project pulling '
    'construction permits from a prior-year approval.',
    'Of the 66 ADUs, only 1 has been issued. 26 were returned for correction and 23 are '
    'still in "received" status.',
]
for n in notes:
    doc.add_paragraph(n, style='List Bullet')

# ─── Section 4: The Drop ───
doc.add_heading('Total Housing Production: The Collapse in Context', level=2)

doc.add_paragraph(
    'Excluding pre-entitled projects (North River Farms tract lots and Olive Park pulling '
    'from a 2024 approval), the city\'s organic 2026 housing production is:'
)

p = doc.add_paragraph()
run = p.add_run('66 ADUs + 3 SFDs + 1 duplex + 6 multi-family units = 78 units in 5.5 months')
run.bold = True
run.font.size = Pt(12)

doc.add_paragraph()

doc.add_paragraph(
    'Annualized, that is approximately 170 units per year — in a city that averaged '
    '2,290 units per year from 2022 to 2025.'
)

table3 = doc.add_table(rows=4, cols=3, style='Light Shading Accent 1')
table3.alignment = WD_TABLE_ALIGNMENT.CENTER
headers3 = ['Metric', '2025', '2026 (annualized)']
data3 = [
    ['Discretionary housing apps', '18', '~2'],
    ['Ministerial housing units', '~730', '~170 (organic)'],
    ['Total', '~1,700 (incl. APR)', '~370 (est.)'],
]
for i, h in enumerate(headers3):
    cell = table3.rows[0].cells[i]
    cell.text = h
    for p in cell.paragraphs:
        for r in p.runs:
            r.bold = True
for ri, row_data in enumerate(data3):
    for ci, val in enumerate(row_data):
        cell = table3.rows[ri + 1].cells[ci]
        cell.text = val
        if ri == 2:
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True

doc.add_paragraph()

p = doc.add_paragraph()
run = p.add_run(
    'The RHNA target requires roughly 890 units per year. At the current organic pace, '
    'Oceanside will produce less than 20% of its annual obligation.'
)
run.bold = True

# ─── Section 5: The Lag Effect ───
doc.add_heading('The Lag Effect: Building Permits Will Get Worse', level=2)

doc.add_paragraph(
    'Building permits lag discretionary approvals by 1–2 years. The multi-family and '
    'mid/high-rise building permits issued in 2025 came from projects approved in 2022–2024:'
)

lag_examples = [
    'Modera Neptune (BLD HIGH RISE, BLDG25-0374): 360 units, approved 2022 as RD22-00005.',
    'Coast Villas (BLD MID RISE, BLDG25-1204): 56 units, approved 2024 as DB25-00002.',
    'Pacifica townhomes (30 BLD MULTI FAMILY permits): 164 units across 12 phases, '
    'approved 2022 as D22-00013.',
]
for ex in lag_examples:
    doc.add_paragraph(ex, style='List Bullet')

doc.add_paragraph()

doc.add_paragraph(
    'With only one discretionary housing application in 2026, there will be few or no '
    'multi-family building permits to pull in 2027–2028. The 2025 building permit numbers '
    'represent the last wave of construction from the pre-collapse pipeline. When those '
    'projects finish, the building permit pipeline will mirror the discretionary freeze.'
)

# ─── Section 6: Why ───
doc.add_heading('What Caused the Collapse', level=2)

doc.add_paragraph(
    'Three policy actions converged to freeze the discretionary pipeline:'
)

causes = [
    ('D-District Density Cap (CCC Certified February 5, 2026). ',
     'The downtown density cap of 86 du/acre, adopted by the City Council in October 2023 '
     'and certified by the Coastal Commission on February 5, 2026, eliminated the city\'s '
     'most productive housing area. Downtown D-District filings went from 1,946 units in '
     '2022 to zero in 2026. No new D-District housing project has been filed since '
     'certification.'),
    ('SB 79 Exemption Ordinance (Adopted June 3, 2026). ',
     'The city adopted an ordinance deferring SB 79 transit-oriented housing compliance '
     'to 2032, using fabricated "walking path" findings to reclassify parcels near transit '
     'as non-qualifying. This froze development potential on remaining non-downtown parcels '
     'that could have absorbed displaced demand.'),
    ('SSCSP Not Yet Adopted. ',
     'The Smart & Sustainable Corridors Specific Plan, which would rezone 1,172 acres '
     'of corridor land to allow 8,300 new housing units, has not yet been adopted. The '
     'hearing is scheduled for June 24, 2026. Until adoption, corridor parcels remain '
     'zoned for low-density residential and light industrial uses (3.9–7.25 du/acre), '
     'making multifamily development physically impossible under existing zoning.'),
]

for title_text, body_text in causes:
    p = doc.add_paragraph()
    run = p.add_run(title_text)
    run.bold = True
    p.add_run(body_text)

# ─── Section 7: The Only Housing Filing Used State Law ───
doc.add_heading('The Only Housing Filing Bypassed City Process', level=2)

doc.add_paragraph(
    'DB26-00001 (142 units at 1640 Oceanside Boulevard) was filed on June 16, 2026 — '
    'eight days before the SSCSP adoption hearing. The project is located on a parcel '
    'currently zoned RE-B (Residential Estate Bonus, 3.9 du/acre maximum). At 142 units '
    'on approximately 1.4 acres, the project proposes roughly 102 du/acre — 26 times '
    'the current zoning allows.'
)

doc.add_paragraph(
    'This is possible because the developer filed under AB 2011, which provides a '
    'ministerial (by-right) approval pathway for housing projects on commercially zoned '
    'land that meet affordability and labor requirements. The project does not require '
    'City Council approval, Planning Commission review, or CEQA analysis.'
)

p = doc.add_paragraph()
run = p.add_run(
    'The fact that the only housing project filed in Oceanside in 2026 had to use state '
    'law to avoid local review is itself evidence that the local regulatory environment '
    'has become hostile to housing production.'
)
run.bold = True

# ─── Section 8: Data Verification ───
doc.add_heading('Data Verification', level=2)

doc.add_paragraph(
    'The data in this briefing was cross-verified across multiple sources:'
)

verification = [
    'Discretionary projects: City of Oceanside eTRAKiT permit portal, project search '
    '(D-, DB-, RD-prefix applications). All projects for 2018–2026 were individually '
    'verified by direct URL query.',
    'Building permits: City of Oceanside eTRAKiT permit portal, permit search '
    '(BLDG-prefix permits). All 2,577 permits for 2025 and all 998 permits for 2026 '
    'were individually scraped and categorized by permit type.',
    'HCD Annual Progress Report: Table A data from data.ca.gov, filtered to '
    'JURIS_NAME=OCEANSIDE, covering 2018–2025. 2026 APR data will not be available '
    'until mid-2027.',
    'Cross-reference: 45 of 50 large APR projects (50+ units, 2018–2025) were matched '
    'to eTRAKiT discretionary applications by Assessor Parcel Number (APN), confirming '
    'both data sources track the same projects.',
    'The 5 unmatched projects were traced to APN discrepancies between APR and eTRAKiT '
    '(different parcel numbers for the same site), not missing projects.',
]
for v in verification:
    doc.add_paragraph(v, style='List Bullet')

# ─── Section 9: Implications ───
doc.add_heading('Implications', level=2)

implications = [
    'RHNA compliance: At the current organic production rate (~170 units/year), Oceanside '
    'will produce less than 20% of its annual RHNA obligation, accumulating a deficit that '
    'makes the 2029 deadline unreachable.',
    'Builder\'s Remedy exposure: If the city fails to maintain adequate housing element '
    'progress, it risks losing its certified housing element status, triggering Builder\'s '
    'Remedy (Government Code 65589.5(d)), which allows developers to bypass local zoning '
    'entirely.',
    'SSCSP urgency: The corridor plan is now the only mechanism that could restore '
    'discretionary housing capacity. Delaying adoption or imposing additional cost '
    'mandates (such as a 20% inclusionary rate) would extend the production freeze.',
    'The lag effect: Even if the SSCSP is adopted on June 24 and developers begin filing '
    'immediately, those projects will not pull building permits until 2027–2028. The '
    'construction gap is already locked in for at least 18 months.',
    'State enforcement: HCD has authority to revoke housing element certification for '
    'jurisdictions that adopt policies inconsistent with their housing element. The '
    'combination of the D-District cap, SB 79 deferment, and pipeline collapse may '
    'constitute grounds for decertification.',
]
for imp in implications:
    doc.add_paragraph(imp, style='List Bullet')

doc.add_paragraph()

# ─── Sources ───
doc.add_heading('Sources', level=2)

sources = [
    'City of Oceanside eTRAKiT permit portal — project search and permit search, '
    'individually verified through June 19, 2026',
    'HCD Annual Progress Report Table A (data.ca.gov) — 2018–2025 filing data',
    'LCPA22-00002 (Downtown density cap ordinance) — adopted October 2023, '
    'CCC certified February 5, 2026',
    'Oceanside SB 79 exemption ordinance — adopted June 3, 2026',
    'Smart & Sustainable Corridors Specific Plan, Hearing Draft (May 8, 2026)',
    'DB26-00001 application record — eTRAKiT, filed June 16, 2026',
    'AB 2011 (Affordable Housing and High Road Jobs Act of 2022) — '
    'Government Code 65912.100 et seq.',
]
for s in sources:
    doc.add_paragraph(s, style='List Bullet')

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/Oceanside 2026 Housing Pipeline Collapse.docx'
doc.save(out)
print(f'Saved: {out}')
