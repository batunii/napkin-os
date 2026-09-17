# Cannes client → category: creative-director ruling

**Status: SIGNED OFF, 2026-09-17.** Contract v1.2.0 is now `"locked": true`. Changing a
category value from here means reprocessing the corpus, so it takes another argument with
a creative director, not a tidy-up.

## The ruling

Five of the seven flagged calls were upheld and two assignments changed. Three values were
added to the closed list before locking.

| Flagged call | Ruling | Reasoning |
| --- | --- | --- |
| Instacart | **keep** `retail` | The work is about picking bananas at the right ripeness. That is a grocery problem, and Tesco precedent is what a planner wants beside it. |
| Uber Eats | **keep** `food_drink` | It sells the takeaway occasion; the competitive set is Domino's and KFC, not Ocado. The apparent inconsistency with Instacart is the rule working: they sell different things. Do not "fix" it. |
| The RealReal | **change** to `luxury` | "Authentication is a retail-trust problem" described the mechanism, not the market. It sells pre-owned designer fashion. A luxury planner wants it; a supermarket planner never would. |
| Specsavers | **keep** `healthcare` | This entry is about hearing loss and driving hearing tests. Per-campaign classification is the whole point. A price-led Specsavers piece would be `retail`. |
| Pedigree | **keep** `fmcg` | Pet food has FMCG economics — penetration, distinctiveness, pack. `food_drink` should mean what humans eat, or the bucket stops meaning anything. |
| Paris 2024 | **keep** `media_entertainment` | The client is the organising committee and the work is a spectacle. IPA files the Rugby League World Cup and Formula 1 the same way. |
| Indian Railways | **keep** `travel` | The problem is fare evasion and the mechanic is a lottery ticket — an operator's commercial problem. State ownership is a shareholder fact, not a briefing fact. |

One further change, outside the seven:

| Entry | Change | Reasoning |
| --- | --- | --- |
| cannes_0021 La Union Newspaper + Article 19 | `media_entertainment` → **`charity`** | Article 19 is a press-freedom NGO and the work is advocacy about murdered journalists — structurally identical to Asuniwa. Filing it by its newspaper co-client repeats the error of filing Indian Railways by who owns it. |

## Added to the closed list before locking

| Value | Why |
| --- | --- |
| `gambling_betting` | Own regulator, own creative red lines. Must never sit inside `media_entertainment` where a planner trips over it unexpectedly. Zero entries today; the bucket costs nothing and prevents a mis-file later. |
| `luxury` | Scarcity, restraint, no price — it behaves as a category, not as expensive fashion. The RealReal is exactly the case. |
| `b2b` | Not a sector, but agencies brief against it as if it were, and the IPA gives it its own award. |

Kept deliberately separate: `fmcg` / `food_drink` / `alcohol`. Alcohol has its own
regulator, media restrictions and conventions, and a beer planner never wants toilet-paper
precedent. `other` should stay near-empty — above 5% of the corpus means this list is wrong.

## The warning attached to the sign-off

> The biggest risk is not a wrong row; it is that the filter gets trusted as a substitute
> for judgement. Category is the weakest predictor of whether a case is useful precedent.
> The best precedent for a bank brief is often a beer campaign that solved the same
> *problem* — a low-interest category, a distinctiveness deficit, a behaviour that needs a
> nudge. A planner who filters `financial_services` and gets Coinbase, AXA, Nordea and
> Rocket has retrieved a sector, not an insight.

Acted on: when the filter is too narrow, `brief_context` now gives up **category before**
`effectiveness_type`. A launch case from another sector beats an automotive case that is
not a launch. See the widening ladder in `brief_context.build()`.

## Method

Every row was assigned by reading that entry's own Brand Context in the corpus, then
checking the call against how the IPA corpus classifies analogous clients (IPA sectors
come from the awarding body). Nothing was assigned from memory alone and nothing was
guessed — a client that could not be placed would have been left unknown.

## Distribution

| Category | Entries |
| --- | --- |
| retail | 12 |
| food_drink | 11 |
| technology | 9 |
| financial_services | 9 |
| media_entertainment | 7 |
| travel | 5 |
| charity | 4 |
| fmcg | 4 |
| alcohol | 4 |
| healthcare | 3 |
| fashion_beauty | 3 |
| luxury | 1 |
| telecoms | 1 |

All 73 entries resolved.

## Every assignment

| Entry | Client | Work | Lions category | Category |
| --- | --- | --- | --- | --- |
| cannes_0001 | APPLE | A CRITTER CAROL | Film | `technology` |
| cannes_0002 | THE REALREAL | L’ULTIMO UOMO REALE ("THE LAST REAL MA | Film | `luxury` ←changed |
| cannes_0003 | ELECTRONIC ARTS SKATE | DROP IN | Film | `media_entertainment` |
| cannes_0004 | COINBASE | YOUR WAY OUT | Film | `financial_services` |
| cannes_0005 | THAI LIFE INSURANCE | UNFADING LOVE | Film | `financial_services` |
| cannes_0006 | JOHN LEWIS & PARTNERS | TABLEAU | Film | `retail` |
| cannes_0007 | SHIELD INSURANCE | AFTER SHIT HAPPENED SERVICE | Film | `financial_services` |
| cannes_0008 | LIFE360 | I THINK OF YOU DYING | Film | `technology` |
| cannes_0009 | FUCK CANCER | BEAT CANCER OFF | Film | `charity` |
| cannes_0010 | INSTACART | FOR PAPA! | Film | `retail` |
| cannes_0011 | APPLE | I'M NOT REMARKABLE | Film | `technology` |
| cannes_0012 | CLAUDE | A TIME AND A PLACE | Film | `technology` |
| cannes_0013 | AXA FRANCE | AXA – NOTHING STOPS WOMEN'S RUGBY | Film | `financial_services` |
| cannes_0014 | ANDREX | FIRST SCHOOL POO | Film | `fmcg` |
| cannes_0015 | APPLE | SLIDE | Film | `technology` |
| cannes_0016 | ELI LILLY | NEVER OVER | Film | `healthcare` |
| cannes_0017 | LILLY | GOOD DAYS | Film | `healthcare` |
| cannes_0018 | JOHN LEWIS & PARTNERS | TABLEAU | Film | `retail` |
| cannes_0019 | INTERMARCHÉ | UNLOVED | Film | `retail` |
| cannes_0020 | AMAZON | BRING A BOOK TO LIFE | Film | `retail` |
| cannes_0021 | LA UNION NEWSPAPER AND ARTICLE 19 | BULLET MACHINE | Film | `charity` ←changed |
| cannes_0022 | OPEN AI | DISH | Film | `technology` |
| cannes_0023 | OPEN AI | PULL UP | Film | `technology` |
| cannes_0024 | OPEN AI | TRIP | Film | `technology` |
| cannes_0025 | DOVE | REAL BEAUTY: HOW A SOAP BRAND CREATED  | Creative Strategy | `fashion_beauty` |
| cannes_0026 | ASUNIWA | SATO 2531 | Creative Strategy | `charity` |
| cannes_0027 | PROCOLOMBIA | HUMANIMAL TOURISM | Creative Strategy | `travel` |
| cannes_0028 | AXA | AXA - THREE WORDS | Creative Strategy | `financial_services` |
| cannes_0029 | ANNAHAR NEWSPAPER | THE NEW PRESIDENT | Creative Strategy | `media_entertainment` |
| cannes_0030 | PEDIGREE | PEDIGREE CARAMELO | Creative Strategy | `fmcg` |
| cannes_0031 | APPLE | SHOT ON IPHONE | Creative Effectiveness | `technology` |
| cannes_0032 | XBOX | THE EVERYDAY TACTICIAN: HOW GETTING A  | Creative Effectiveness | `media_entertainment` |
| cannes_0033 | HERTOG JAN | DON'T DRINK HERTOG JAN | Creative Effectiveness | `alcohol` |
| cannes_0034 | SPECSAVERS | THE MISHEARD VERSION | Creative Effectiveness | `healthcare` |
| cannes_0035 | AXA | AXA - THREE WORDS | Direct | `financial_services` |
| cannes_0036 | MERCADO LIVRE | COUPON RAIN | Direct | `retail` |
| cannes_0037 | IKEA | U UP? | Direct | `retail` |
| cannes_0038 | PENNY | PRICE PACKS | Direct | `retail` |
| cannes_0039 | UBER EATS | FOOTBALL IS FOR FOOD | Direct | `food_drink` |
| cannes_0040 | LIDL | LIDLIZE | Direct | `retail` |
| cannes_0041 | KFC | PRIZE ON THE BONE | Direct | `food_drink` |
| cannes_0042 | INDIAN RAILWAYS | LUCKY YATRA | Direct | `travel` |
| cannes_0043 | IKEA | IKEA HIDDEN TAGS | Direct | `retail` |
| cannes_0044 | ANGEL SOFT | POTTY-TUNITIES | Direct | `fmcg` |
| cannes_0045 | ZIPLOC | PRESERVED PROMOS | Direct | `fmcg` |
| cannes_0046 | INDIAN RAILWAYS | LUCKY YATRA | PR | `travel` |
| cannes_0047 | NORDEA | THE PARENTAL LEAVE MORTGAGE | PR | `financial_services` |
| cannes_0048 | INDIAN RAILWAYS | LUCKY YATRA | PR | `travel` |
| cannes_0049 | CLASH OF CLANS | HAALAND PAYBACK TIME | PR | `media_entertainment` |
| cannes_0050 | NUTTER BUTTER | NUTTER BUTTER, YOU GOOD? | PR | `food_drink` |
| cannes_0051 | AXA | AXA - THREE WORDS | PR | `financial_services` |
| cannes_0052 | PROGRESSO | PROGRESSO SOUP DROPS | PR | `food_drink` |
| cannes_0053 | CORONA | SUN RESERVE | PR | `alcohol` |
| cannes_0054 | O2 | DAISY VS SCAMMERS | PR | `telecoms` |
| cannes_0055 | DOVE | DOVE REAL BEAUTY REDEFINED FOR THE AI  | Media | `fashion_beauty` |
| cannes_0056 | SKOL | RETRO INFLUENCERS | Media | `alcohol` |
| cannes_0057 | HEINZ KETCHUP & MUSTARD | CAN'T UNSEE IT | Media | `food_drink` |
| cannes_0058 | HBO | “RAISE YOUR BANNERS” | Media | `media_entertainment` |
| cannes_0059 | ROCKET | FIRST EVER LIVE COMMERCIAL CROSSOVER | Media | `financial_services` |
| cannes_0060 | MERCADO LIVRE | COUPON RAIN | Media | `retail` |
| cannes_0061 | KITKAT | STREET | Outdoor | `food_drink` |
| cannes_0062 | KITKAT | PUBLIC TRANSPORT | Outdoor | `food_drink` |
| cannes_0063 | KITKAT | CROWD | Outdoor | `food_drink` |
| cannes_0064 | PARIS 2024 | OLYMPIC GAMES OPENING CEREMONY PARIS 2 | Outdoor | `media_entertainment` |
| cannes_0065 | ITV X CALM | MISSED BIRTHDAYS | Outdoor | `charity` |
| cannes_0066 | PENNY | PRICE PACKS | Outdoor | `retail` |
| cannes_0067 | AXE/LYNX | SCRATCH & SNIFF | Outdoor | `fashion_beauty` |
| cannes_0068 | HEINZ | TOAST | Outdoor | `food_drink` |
| cannes_0069 | INDIAN RAILWAYS | LUCKY YATRA | Outdoor | `travel` |
| cannes_0070 | STELLA ARTOIS | PROTECTOR OF THE CHALICE | Outdoor | `alcohol` |
| cannes_0071 | HEINZ | CHIPS | Outdoor | `food_drink` |
| cannes_0072 | COCA-COLA | SHADES OF RED | Outdoor | `food_drink` |
| cannes_0073 | RIMAS MUSIC | TRACKING BAD BUNNY | Outdoor | `media_entertainment` |

Regenerate from the corpus and `normalise.CLIENT_TO_CATEGORY`, then:

```
cd engine/rag && python3 rag.py retag --corpus <corpus>/rag --index ./_index_v3 --apply
```

`retag` rewrites metadata without re-embedding, because category is filtered on,
never embedded.
