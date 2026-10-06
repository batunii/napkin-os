# Rule pack sources

Each table lists every rule in one pack in `engine/rule_packs/`. Doubts come first. All packs are `reviewed: false`: Sai flips that only after checking every row.

**How the quotes were checked.** Each page or PDF was downloaded on 2026-10-03 (PDFs converted with `pdftotext`). A script then confirmed that every `quote` appears word for word in the fetched text, ignoring only whitespace, line breaks and the curly-versus-straight quote-mark style. Every rule passed; no rule relies on paraphrase. Limits checked: `rule` is at most 45 words and `quote` at most 40.

**Sources that could not be fetched (rules dropped or rerouted):** legislatie.just.ro (empty reply), anpc.ro and onjn.gov.ro (JavaScript bot wall), cdep.ro (404), EUR-Lex (bot challenge; the Directive text came from the EU Publications Office cellar instead).

## Ireland (`ie.yaml`, kind market, 12 rules)

**Doubts first:**

- ASA (formerly ASAI, now 'Advertising Standards Authority') Code 7th edition is still the one published, but ASA ran a 2026 consultation on a refreshed Code. A new edition may renumber sections.
- The Directive 2024/825 rules (generic green claims, offsetting claims) are quoted from the EU Directive. I could not find the Irish transposing S.I.; DETE's page says the rules apply from 27 Sept 2026.
- S.I. 774/2007 was read in its 'made' (original) form; amendments were not checked.
- Dropped: Consumer Protection Act 2007 s.43 general misleading test (covered by ASA 4.1 and S.I. 774 reg 4(2)(b)); left out to stay within 12 rules, not because of sourcing.

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `ie-legal-decent-honest-truthful` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.3.2 (see also 3.5)) | https://adstandards.ie/code/general-rules/ | Quote matched verbatim in fetched text |  |
| `ie-claim-substantiation` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.4.10) | https://adstandards.ie/code/misleading-advertising/ | Quote matched verbatim in fetched text |  |
| `ie-superiority-claims` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.4.33 (see also 4.31)) | https://adstandards.ie/code/misleading-advertising/ | Quote matched verbatim in fetched text |  |
| `ie-comparative-advertising` | IE / all | European Communities (Misleading and Comparative Marketing Communications) Regulations 2007 (S.I. No. 774/2007) (reg. 4(2)(c)-(d)) | https://www.irishstatutebook.ie/eli/2007/si/774/made/en/print | Quote matched verbatim in fetched text |  |
| `ie-ad-recognisability` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.3.32 (see also 3.31, 3.33)) | https://adstandards.ie/code/general-rules/ | Quote matched verbatim in fetched text |  |
| `ie-influencer-ad-label` | IE / online | Social Media Influencer Guidance (Primary labels) | https://adstandards.ie/social-media-influencers/ | Quote matched verbatim in fetched text | Guidance page, not Code text. |
| `ie-paid-editorial-disclosure` | IE / all | Consumer Protection Act 2007 (revised, Law Reform Commission) (s.55(1)(q)) | https://revisedacts.lawreform.ie/eli/2007/act/19/section/55/revised/en/html | Quote matched verbatim in fetched text | Revised Act current to 24 Nov 2025; LRC flags later unimplemented changes. |
| `ie-children-no-pester-power` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.7.5(c)) | https://adstandards.ie/code/children/ | Quote matched verbatim in fetched text |  |
| `ie-broadcast-childrens-ads-banned-categories` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.11.2) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `ie-environmental-claims-substantiation` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.15.2 (see also 15.5-15.6 on basis and lifecycle)) | https://adstandards.ie/code/environmental-claims/ | Quote matched verbatim in fetched text |  |
| `ie-generic-environmental-claims-ban` | IE / all | Directive (EU) 2024/825 (Empowering Consumers for the Green Transition), amending Annex I to Directive 2005/29/EC (Annex, point 4a (new Annex I item to Directive 2005/29/EC)) | http://publications.europa.eu/resource/celex/32024L0825 | Quote matched verbatim in fetched text | EU text; national transposing measure not located. |
| `ie-offsetting-based-climate-claims-ban` | IE / all | Directive (EU) 2024/825 (Empowering Consumers for the Green Transition), amending Annex I to Directive 2005/29/EC (Annex, point 4c (new Annex I item to Directive 2005/29/EC)) | http://publications.europa.eu/resource/celex/32024L0825 | Quote matched verbatim in fetched text | EU text; national transposing measure not located. |

## Great Britain (`gb.yaml`, kind market, 12 rules)

**Doubts first:**

- CAP/BCAP rule numbers are from asa.org.uk on 2026-10-03, after the DMCCA changes (April 2025) and the 24 Oct 2025 deletions in the environmental sections.
- Most GB rules cite CAP (non-broadcast) with `media: all`; the matching BCAP rule is named in `note`. Check you are happy with one rule covering both codes.

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `gb-no-misleading` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 3.1 (see also 3.3)) | https://www.asa.org.uk/type/non_broadcast/code_section/03.html | Quote matched verbatim in fetched text |  |
| `gb-claim-substantiation` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 3.7) | https://www.asa.org.uk/type/non_broadcast/code_section/03.html | Quote matched verbatim in fetched text |  |
| `gb-superlative-claims` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (Comparisons: Principle) | https://www.asa.org.uk/type/non_broadcast/code_section/03.html | Quote matched verbatim in fetched text |  |
| `gb-comparative-advertising` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 3.34 (see also 3.32-3.35, 3.41)) | https://www.asa.org.uk/type/non_broadcast/code_section/03.html | Quote matched verbatim in fetched text |  |
| `gb-ad-recognisability` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 2.1 (see also 2.3)) | https://www.asa.org.uk/type/non_broadcast/code_section/02.html | Quote matched verbatim in fetched text |  |
| `gb-advertorial-label` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 2.4) | https://www.asa.org.uk/type/non_broadcast/code_section/02.html | Quote matched verbatim in fetched text |  |
| `gb-reviews-fake-or-incentivised` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 3.45 (see also 3.44, 3.46)) | https://www.asa.org.uk/type/non_broadcast/code_section/03.html | Quote matched verbatim in fetched text |  |
| `gb-children-direct-appeal` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 5.4.2) | https://www.asa.org.uk/type/non_broadcast/code_section/05.html | Quote matched verbatim in fetched text |  |
| `gb-environmental-claims-basis` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 11.1 (see also 11.2)) | https://www.asa.org.uk/type/non_broadcast/code_section/11.html | Quote matched verbatim in fetched text |  |
| `gb-environmental-absolute-and-lifecycle` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 11.3 (see also 11.4)) | https://www.asa.org.uk/type/non_broadcast/code_section/11.html | Quote matched verbatim in fetched text |  |
| `gb-broadcast-clearance` | GB / broadcast | UK Code of Broadcast Advertising (BCAP Code) (Introduction, para (e) (see also Section 1 background)) | https://www.asa.org.uk/type/broadcast/code_folder/introduction.html | Quote matched verbatim in fetched text |  |
| `gb-tv-no-newsreaders` | GB / broadcast | UK Code of Broadcast Advertising (BCAP Code) (rule 2.4.2 (see also 2.1, 2.4.1)) | https://www.asa.org.uk/type/broadcast/code_section/02.html | Quote matched verbatim in fetched text |  |

## Romania (`ro.yaml`, kind market, 12 rules)

**Doubts first:**

- legislatie.just.ro (the official portal), anpc.ro and onjn.gov.ro refused automated access (empty reply or bot wall). Law 158/2008 comes from a government-hosted consolidated PDF valid at 19 Feb 2018, so later amendments are unchecked.
- CNA Decision 220/2011 (named in the brief) has been REPLACED by CNA Decision 573/2025, in force 7 Aug 2025 (Art. 174 repeals 220/2011). The pack cites 573/2025 as consolidated on 11.12.2025.
- The RAC Code is self-regulation (version approved 15 Dec 2025). It binds RAC members, not everyone.
- Transposition of Directive 2024/825 in Romania was not checked.
- The 'P' symbol date (ro-broadcast-ad-marking) is my calculation: 6 months after 7 Aug 2025. Confirm it with CNA.

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `ro-honest-truthful-decent` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 1.1) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-claim-substantiation` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 7.1) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-unproven-claims-deemed-inaccurate` | RO / all | Legea nr. 158/2008 privind publicitatea înşelătoare şi publicitatea comparativă (republicată 2013; consolidated form valid 19.02.2018) (Art. 9(5)) | https://stk8sprevenirep01wp01.blob.unit13.cloudgov.ro/uploads/2018/04/LEGEA-158-DIN-2008.pdf | Quote matched verbatim in fetched text | Consolidated text valid at 19 Feb 2018; official portal blocked. |
| `ro-comparative-advertising` | RO / all | Legea nr. 158/2008 privind publicitatea înşelătoare şi publicitatea comparativă (republicată 2013; consolidated form valid 19.02.2018) (Art. 6 lit. c)) | https://stk8sprevenirep01wp01.blob.unit13.cloudgov.ro/uploads/2018/04/LEGEA-158-DIN-2008.pdf | Quote matched verbatim in fetched text | Consolidated text valid at 19 Feb 2018; official portal blocked. |
| `ro-no-denigration` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 13) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-ad-recognisability` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 1.9 (see also 5, 15.4-15.5)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-influencer-disclosure` | RO / online | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 4.2 (see also 4.1)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-broadcast-ad-marking` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 130(3) lit. a)-c)) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text | Date of the switch to the 'P' symbol is my calculation. |
| `ro-broadcast-no-news-presenters` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 130(3) lit. d)) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text |  |
| `ro-children-no-pester-power` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 9.10 (see also 9.1-9.12)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-environmental-claims-scope` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 16.3 (see also 16.2, 16.4)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `ro-generic-environmental-claims-ban` | RO / all | Directive (EU) 2024/825 (Empowering Consumers for the Green Transition), amending Annex I to Directive 2005/29/EC (Annex, point 4a (new Annex I item to Directive 2005/29/EC)) | http://publications.europa.eu/resource/celex/32024L0825 | Quote matched verbatim in fetched text | EU text; RO transposition not checked. |

## Alcohol (`alcohol.yaml`, kind category, 12 rules)

**Doubts first:**

- PHAA s.13 is NOT commenced (ISB directory updated 17 Sept 2026). That covers the health warnings in ads (13(2)) and the limits on ad content (13(7)-(11)). So no rule cites s.13; recheck before each use.
- PHAA s.18 (publications, 20% limits, front/back covers) is NOT commenced, so it is not used.
- Left out for space (sourced, in force): PHAA s.15 (no alcohol ads in sports areas during events or at children's events), s.16 (sponsorship of children's or motor events), CAP 18.15 (25% under-18 audience cap), BCAP 32.2.1 and RAC 27.2(a).
- RO: Law 143/2000 is a drugs law and has nothing on alcohol ads. Law 148/2000 on advertising (alcohol provisions) could not be fetched because anpc.ro has a bot wall. RO alcohol rules therefore rest on CNA 573/2025 and RAC.

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `alcohol-ie-broadcast-watershed` | IE / broadcast | Public Health (Alcohol) Act 2018 (s.19(1)-(2)) | https://www.irishstatutebook.ie/eli/2018/act/24/enacted/en/print | Quote matched verbatim in fetched text; commencement checked on ISB directory |  |
| `alcohol-ie-outdoor-restricted-places` | IE / outdoor | Public Health (Alcohol) Act 2018 (s.14(1)) | https://www.irishstatutebook.ie/eli/2018/act/24/enacted/en/print | Quote matched verbatim in fetched text; commencement checked on ISB directory |  |
| `alcohol-ie-cinema` | IE / cinema | Public Health (Alcohol) Act 2018 (s.20(2)) | https://www.irishstatutebook.ie/eli/2018/act/24/enacted/en/print | Quote matched verbatim in fetched text; commencement checked on ISB directory |  |
| `alcohol-ie-broadcast-no-spirits` | IE / broadcast | Media Service Code: General Commercial Communications Code (in effect 5 November 2024) (s.18.3) | https://www.cnam.ie/app/uploads/2025/01/General-Commercial-Communications-Code-v2.pdf | Quote matched verbatim in fetched text |  |
| `alcohol-ie-responsibility-message` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.9.4) | https://adstandards.ie/code/alcoholic-drinks/ | Quote matched verbatim in fetched text; commencement checked on ISB directory | ASA self-regulatory; statutory warnings (s.13) not commenced. |
| `alcohol-ie-over-25` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.9.7(a)) | https://adstandards.ie/code/alcoholic-drinks/ | Quote matched verbatim in fetched text |  |
| `alcohol-gb-under-25` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 18.16) | https://www.asa.org.uk/type/non_broadcast/code_section/18.html | Quote matched verbatim in fetched text |  |
| `alcohol-gb-no-under-18-appeal` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 18.14 (see also 18.15)) | https://www.asa.org.uk/type/non_broadcast/code_section/18.html | Quote matched verbatim in fetched text |  |
| `alcohol-gb-no-sexual-success` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 18.5 (see also 18.2-18.7)) | https://www.asa.org.uk/type/non_broadcast/code_section/18.html | Quote matched verbatim in fetched text |  |
| `alcohol-ro-spirits-broadcast-hours` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 138(1)) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text |  |
| `alcohol-ro-spirits-warning` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 141) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text | Whether the duty sits on the spot or the broadcaster's break is unclear. |
| `alcohol-ro-over-25` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 27.4 (see also 27.2 lit. a)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text | RAC self-regulation; statute (Law 148/2000) not fetched. |

## Gambling and betting (`gambling.yaml`, kind category, 12 rules)

**Doubts first:**

- IE Gambling Regulation Act 2024 advertising sections 143-151 (watershed, content, social media) are NOT commenced per the ISB directory (updated 17 Sept 2026). No rule cites them; they will change this pack when commenced.
- RO: GEO 77/2009 and the ONJN rules could not be fetched (onjn.gov.ro and legislatie.just.ro bot walls). The mandatory-elements rule cites the RAC Code, which mirrors them. Verify against the statute.
- GB: the industry's voluntary 'whistle-to-whistle' TV ban and the Gambling Commission's LCCP marketing conditions (e.g. bonus/wagering limits) were not sourced and are not included.
- Left out for space: ASA IE 10.17(a) (particular appeal to children) and 10.12 (a),(b),(d)-(k).

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `gambling-ie-safer-gambling-message` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.10.10) | https://adstandards.ie/code/gambling/ | Quote matched verbatim in fetched text; commencement checked on ISB directory | GRA 2024 ss.143-151 not commenced; recheck. |
| `gambling-ie-under-25` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.10.17(f) (see also 10.17(e))) | https://adstandards.ie/code/gambling/ | Quote matched verbatim in fetched text |  |
| `gambling-ie-no-financial-solution` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.10.12(c) (see also 10.12(a)-(k))) | https://adstandards.ie/code/gambling/ | Quote matched verbatim in fetched text |  |
| `gambling-ie-schools-100m` | IE / outdoor | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.10.17(h)) | https://adstandards.ie/code/gambling/ | Quote matched verbatim in fetched text |  |
| `gambling-ie-broadcast-no-inducements` | IE / broadcast | Media Service Code: General Commercial Communications Code (in effect 5 November 2024) (s.25.4) | https://www.cnam.ie/app/uploads/2025/01/General-Commercial-Communications-Code-v2.pdf | Quote matched verbatim in fetched text |  |
| `gambling-ie-broadcast-children` | IE / broadcast | Media Service Code: General Commercial Communications Code (in effect 5 November 2024) (s.25.6) | https://www.cnam.ie/app/uploads/2025/01/General-Commercial-Communications-Code-v2.pdf | Quote matched verbatim in fetched text |  |
| `gambling-gb-no-strong-under-18-appeal` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 16.3.12) | https://www.asa.org.uk/type/non_broadcast/code_section/16.html | Quote matched verbatim in fetched text |  |
| `gambling-gb-under-25` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 16.3.14) | https://www.asa.org.uk/type/non_broadcast/code_section/16.html | Quote matched verbatim in fetched text |  |
| `gambling-gb-no-financial-solution` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 16.3.4 (see also 16.3.1-16.3.17)) | https://www.asa.org.uk/type/non_broadcast/code_section/16.html | Quote matched verbatim in fetched text |  |
| `gambling-ro-broadcast-hours` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 109(3)) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text |  |
| `gambling-ro-no-celebrities-influencers` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 109(7)) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text |  |
| `gambling-ro-mandatory-elements` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 37.3) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text | RAC text mirrors GEO 77/2009, which I could not fetch. |

## Less healthy (HFSS) food and drink (`hfss_food.yaml`, kind category, 12 rules)

**Doubts first:**

- GB 'less healthy food' (LHF) rules have been in force since 5 Jan 2026 (GOV.UK). The product scope depends on the 2024 Regulations categories plus the 2011 nutrient profiling model, and pure brand ads are exempt. The brief should flag 'check product is in scope'.
- On-demand TV: GOV.UK says the 9pm watershed covers ODPS. The CAP Appendix 2 rule 30.16 text was not fetched.
- IE ASA HFSS Guidance Note (media-specific thresholds) was not fetched.
- Left out for space: CAP 15.14 / BCAP 13.9 (promotional offers to young children) and ASA 8.22 / 8.26 (locations, licensed characters non-broadcast).

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `hfss-gb-online-paid-ban` | GB / online | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 15.19) | https://www.asa.org.uk/type/non_broadcast/code_section/15.html | Quote matched verbatim in fetched text; in-force date checked | Scope/exemptions complex (brand, SME, product categories). |
| `hfss-gb-tv-9pm-watershed` | GB / broadcast | UK Code of Broadcast Advertising (BCAP Code) (rule 32.21) | https://www.asa.org.uk/type/broadcast/code_section/32.html | Quote matched verbatim in fetched text; in-force date checked |  |
| `hfss-gb-non-broadcast-audience-25pct` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 15.20) | https://www.asa.org.uk/type/non_broadcast/code_section/15.html | Quote matched verbatim in fetched text |  |
| `hfss-gb-tv-childrens-programmes` | GB / broadcast | UK Code of Broadcast Advertising (BCAP Code) (rule 32.22) | https://www.asa.org.uk/type/broadcast/code_section/32.html | Quote matched verbatim in fetched text |  |
| `hfss-gb-no-licensed-characters` | GB / all | UK Code of Non-broadcast Advertising and Direct & Promotional Marketing (CAP Code) (rule 15.15 (see also 15.14 on promotional offers)) | https://www.asa.org.uk/type/non_broadcast/code_section/15.html | Quote matched verbatim in fetched text |  |
| `hfss-ie-broadcast-not-in-childrens-programmes` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.17.7(a)) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `hfss-ie-broadcast-no-licensed-characters` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.17.7(b)) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `hfss-ie-broadcast-under-13-no-claims-or-offers` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.17.7(c)-(d)) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `hfss-ie-broadcast-no-celebrities-under-15` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.17.6) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `hfss-ie-broadcast-fast-food-message` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.17.4) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `hfss-ie-broadcast-confectionery-message` | IE / broadcast | Media Service Code: Children's Commercial Communications Code (in effect 6 December 2024) (s.17.5) | https://www.cnam.ie/app/uploads/2024/12/Childrens-Commercial-Communications-Code-December-2024.pdf | Quote matched verbatim in fetched text |  |
| `hfss-ie-non-broadcast-audience-50pct` | IE / all | Code of Standards for Advertising and Marketing Communications in Ireland, 7th edition (ASA Code) (s.8.20 (see also 8.19, 8.21-8.22)) | https://adstandards.ie/code/food-non-alcoholic-beverages/ | Quote matched verbatim in fetched text |  |

## Charity and fundraising (`charity_fundraising.yaml`, kind category, 12 rules)

**Doubts first:**

- The IE Charities Regulator fundraising guidelines date from 2017 and are best-practice guidelines, not a statutory code. I could not confirm whether newer guidance exists under the Charities (Amendment) Act 2024; the regulator's HTML pages did not load by script, only the PDF.
- The UK Code of Fundraising Practice (in effect 1 Nov 2025) covers England, Wales and NI, not Scotland (OSCR), but the rules are tagged `GB`.
- The CAP Code (non-broadcast) charity-linked promotion rules (Section 8) were not fetched. The cause-related-amount rule therefore cites BCAP 16.9 and is tagged broadcast.
- Two-column PDF: the Charities Regulator quotes were checked against a reading-order text extraction, so the column text was not read across lines.

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `charity-ie-no-misrepresentation` | IE / all | Guidelines for Charitable Organisations on Fundraising from the Public (2017) (s.3 Core principles: Honesty and Integrity) | https://www.charitiesregulator.ie/media/o5ul004d/guidance-for-fundraising-english.pdf | Quote matched verbatim in fetched text | 2017 non-statutory guidelines. |
| `charity-ie-restricted-purpose` | IE / all | Guidelines for Charitable Organisations on Fundraising from the Public (2017) (s.7 Responsibilities of Fundraisers) | https://www.charitiesregulator.ie/media/o5ul004d/guidance-for-fundraising-english.pdf | Quote matched verbatim in fetched text |  |
| `charity-ie-beneficiary-dignity` | IE / all | Guidelines for Charitable Organisations on Fundraising from the Public (2017) (s.3 Core principles: Respect) | https://www.charitiesregulator.ie/media/o5ul004d/guidance-for-fundraising-english.pdf | Quote matched verbatim in fetched text |  |
| `charity-ie-identify-cause` | IE / all | Guidelines for Charitable Organisations on Fundraising from the Public (2017) (s.3 Core principles: Transparency and Accountability) | https://www.charitiesregulator.ie/media/o5ul004d/guidance-for-fundraising-english.pdf | Quote matched verbatim in fetched text |  |
| `charity-gb-not-misleading` | GB / all | Code of Fundraising Practice (in effect 1 November 2025) (1.2.1 (see also 1.2.3 evidence for claims)) | https://www.fundraisingregulator.org.uk/code/standards-which-apply-all-fundraising/behaviour-when-fundraising | Quote matched verbatim in fetched text |  |
| `charity-gb-shocking-content` | GB / all | Code of Fundraising Practice (in effect 1 November 2025) (8.1.2) | https://www.fundraisingregulator.org.uk/code/standards-which-apply-specific-fundraising-methods/fundraising-communications-and-advertising | Quote matched verbatim in fetched text |  |
| `charity-gb-case-studies-representative` | GB / all | Code of Fundraising Practice (in effect 1 November 2025) (8.1.4) | https://www.fundraisingregulator.org.uk/code/standards-which-apply-specific-fundraising-methods/fundraising-communications-and-advertising | Quote matched verbatim in fetched text |  |
| `charity-gb-case-study-consent` | GB / all | Code of Fundraising Practice (in effect 1 November 2025) (8.1.5) | https://www.fundraisingregulator.org.uk/code/standards-which-apply-specific-fundraising-methods/fundraising-communications-and-advertising | Quote matched verbatim in fetched text |  |
| `charity-gb-broadcast-no-guilt` | GB / broadcast | UK Code of Broadcast Advertising (BCAP Code) (rule 16.3.2 (see also 16.3.1, 16.3.3, 16.3.4)) | https://www.asa.org.uk/type/broadcast/code_section/16.html | Quote matched verbatim in fetched text |  |
| `charity-gb-cause-related-amount` | GB / broadcast | UK Code of Broadcast Advertising (BCAP Code) (rule 16.9 (see also 16.6.2)) | https://www.asa.org.uk/type/broadcast/code_section/16.html | Quote matched verbatim in fetched text | BCAP only; CAP Section 8 not fetched. |
| `charity-ro-no-exploiting-suffering` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 36.3 lit. b)-c)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |
| `charity-ro-name-advertiser-and-aim` | RO / all | Codul de practică în comunicarea comercială (version approved 15 December 2025) (Art. 36.1 (see also 36.2)) | https://www.rac.ro/ro/cod/codul-de-practic%C4%82-%C3%8En-comunicarea-comercial%C4%82 | Quote matched verbatim in fetched text |  |

## Suicide and self-harm (`suicide_self_harm.yaml`, kind topic, 12 rules)

**Doubts first:**

- WHO and Samaritans guidance is written for news and media reporting, not advertising. Each rule's note says to apply it as best practice to ads.
- The Samaritans UK edition I could fetch is from 2017 (the samaritans.org pages served Ireland content), so GB rules rely on the WHO [ALL] rules. The Samaritans IE edition is 2020.
- The Samaritans self-harm guidance PDF is undated.
- Merged for space: Samaritans IE language table ('Don't use: Commit suicide...'). It is mentioned in the note on suicide-avoid-committed.
- RO rule cites CNA 573/2025 Art. 83, which applies to news and debate programmes.

| rule id | markets / media | source (section) | url | what I verified | doubt |
|---|---|---|---|---|---|
| `suicide-no-method` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Don't describe the method used (p.18)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-no-location` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Don't name or provide details about the site/location (p.18)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-not-a-solution` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Don't use language/content which sensationalizes... (p.19)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-avoid-committed` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (p.20) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-signpost-support` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Do provide accurate information about where to seek help (p.12)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-hope-and-recovery` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Do report stories of how to cope... (p.13)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-no-scene-images` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Don't use photographs, video footage... (p.21)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-no-single-cause` | ALL / all | Preventing suicide: a resource for media professionals, update 2023 (Don't oversimplify the reason for a suicide (p.20)) | https://iris.who.int/server/api/core/bitstreams/92773496-dc8b-4d42-aef3-3a5a517dfa63/content | Quote matched verbatim in fetched text |  |
| `suicide-ie-samaritans-helpline` | IE / all | Media Guidelines for Reporting Suicide (Ireland edition, 2020) (10 things to remember, item 2) | https://media.samaritans.org/documents/Media_Guidelines_Ireland_FINAL.pdf | Quote matched verbatim in fetched text | Check helpline details before publishing. |
| `self-harm-ie-no-images` | IE / all | Guidance for covering self-harm in the media (Republic of Ireland) (Best practice for reporting on self-harm) | https://www.samaritans.org/documents/1079/ROI_Guidance_for_covering_self-harm_in_the_media_FINAL.pdf | Quote matched verbatim in fetched text | Undated document. |
| `self-harm-ie-not-a-solution` | IE / all | Guidance for covering self-harm in the media (Republic of Ireland) (Best practice for reporting on self-harm) | https://www.samaritans.org/documents/1079/ROI_Guidance_for_covering_self-harm_in_the_media_FINAL.pdf | Quote matched verbatim in fetched text |  |
| `suicide-ro-broadcast` | RO / broadcast | Decizia CNA nr. 573/2025 privind Codul de reglementare a conținutului audiovizual (consolidated to 11.12.2025) (Art. 83 lit. g)-h)) | https://media.cna.ro/Decizia_CNA_nr_573_2025_Codul_audiovizualului_cu_modificarile_ulterioare_la_data_de_11_12_2025_ab2f6787a8.pdf | Quote matched verbatim in fetched text | News/debate programme rule; best practice for ads. |
