# Cannes client → category: annotation review

**Status:** proposed, awaiting creative-director sign-off (Phase 0). Until this is
signed off, `engine/schema/rag_metadata.v1.json` stays `"locked": false`.

## Why this file exists

The Cannes corpus records `sector: general` on nearly every entry, so 73 award cases
carried no client category and were invisible to any category filter. A brief for an
automotive client would never have seen a Cannes automotive case.

Each row below was assigned by reading that entry's own Brand Context in the corpus,
then checking the call against how the IPA corpus classifies analogous clients (IPA
sectors come from the awarding body, so they are the closest thing to an authority we
hold). Nothing here was assigned from memory alone, and nothing was guessed: a client
we could not place would have been left unknown rather than approximated.

## The rule applied

Classify by what the client **sells in this campaign**, not by the technology it sells
through. The IPA corpus reserves `Technology` for makers of the technology itself (IBM,
Kodak, Motorola, Nikon, Polaroid, Sony Ericsson), so a shop that runs on an app is still
a shop. The corpus also classifies per campaign rather than per client — it carries
Specsavers as both Healthcare and Retail, John Lewis as both Retail and Financial
Services, Tesco as both Retail and Telecoms, Mars as both FMCG and Food & Drink.

## Please review these seven first

These are defensible either way. They are listed so they are argued with rather than
rediscovered later.

| Entry | Call made | The argument against |
| --- | --- | --- |
| INSTACART | retail | It is a delivery platform, not a shop. Classified by what it delivers: groceries. |
| UBER EATS | food_drink | Same business model as Instacart but classified differently, because it delivers restaurant meals rather than groceries. The taxonomy has no home for delivery marketplaces. |
| THE REALREAL | retail | It sells pre-owned designer fashion, so `fashion_beauty` is arguable. The campaign is about authentication, which is a retail-trust problem. |
| SPECSAVERS | healthcare | It is a high-street retailer. IPA carries it both ways; this entry is about hearing loss and driving hearing tests. |
| PEDIGREE | fmcg | Pet food. IPA is itself split: Mars Petcare and Nestle Purina Petcare are FMCG, Friskies Petcare is Food & Drink. |
| PARIS 2024 | media_entertainment | A national event, so `public_sector` is arguable. IPA files sporting events (Rugby League World Cup, Formula 1) under Entertainment & Media; the Olympic *Delivery Authority* is Public Sector, but that body built venues. |
| INDIAN RAILWAYS | travel | State-owned, so `public_sector` is arguable. IPA files rail operators (LNER, Virgin Trains, Eurostar, MTR) under Travel & Tourism and reserves Public Sector for transport authorities like TfL. |

## Distribution

| Category | Entries |
| --- | --- |
| retail | 13 |
| food_drink | 11 |
| technology | 9 |
| financial_services | 9 |
| media_entertainment | 8 |
| travel | 5 |
| fmcg | 4 |
| alcohol | 4 |
| charity | 3 |
| healthcare | 3 |
| fashion_beauty | 3 |
| telecoms | 1 |

All 73 entries resolved. None left unknown.

## Every assignment, with the evidence it was read from

| Entry | Client | Work | Lions category | Assigned | Evidence from the entry |
| --- | --- | --- | --- | --- | --- |
| cannes_0001 | APPLE | A CRITTER CAROL | Film | `technology` | A Critter Carol is a film about doing things the hard way, on purpose. It leans fully into the language of cinema: character, perf… |
| cannes_0002 | THE REALREAL | L’ULTIMO UOMO REALE ("THE LAST REAL MAN" | Film | `retail` ⚑ | We believe this film meets the highest standard of the category. A film that avoids overt branding, allowing the audience to engag… |
| cannes_0003 | ELECTRONIC ARTS SKATE | DROP IN | Film | `media_entertainment` | The approach was to create a true skate tape, rooted in authenticity and made to feel native to skate culture; not like an ad for … |
| cannes_0004 | COINBASE | YOUR WAY OUT | Film | `financial_services` | “Your Way Out” is a film for Coinbase made for the 98th Academy Awards. A story about taking control and leaving the financial sys… |
| cannes_0005 | THAI LIFE INSURANCE | UNFADING LOVE | Film | `financial_services` | Unfading Love is relevant for film because it prioritizes cinematic storytelling over advertising conventions. The brand steps bac… |
| cannes_0006 | JOHN LEWIS & PARTNERS | TABLEAU | Film | `retail` | Film was the only medium capable of capturing a century of British life and John Lewis’ place within it. Tableau is a 100-second f… |
| cannes_0007 | SHIELD INSURANCE | AFTER SHIT HAPPENED SERVICE | Film | `financial_services` | To demonstrate the full capabilities of After Shit Happened Service, we used film to portray a cinematic butterfly effect of bad l… |
| cannes_0008 | LIFE360 | I THINK OF YOU DYING | Film | `technology` | Few emotions are as universal or as complicated as Mom’s worry for her children. We leaned into that fear for this Mother’s Day ca… |
| cannes_0009 | FUCK CANCER | BEAT CANCER OFF | Film | `charity` | Prostate cancer is the second leading cause of cancer death in men, but men ignore serious messages to get screenings. Fuck Cancer… |
| cannes_0010 | INSTACART | FOR PAPA! | Film | `retail` ⚑ | This work uses film not just to show a product, but to tell a story audiences want to watch. It transforms a simple feature into a… |
| cannes_0011 | APPLE | I'M NOT REMARKABLE | Film | `technology` | The film is an all-singing, all-dancing college based musical, starring a cast of students with disabilities delivering an importa… |
| cannes_0012 | CLAUDE | A TIME AND A PLACE | Film | `technology` | Four films used advertising’s most-watched stage to argue against advertising in AI. Darkly comedic and designed for cultural impa… |
| cannes_0013 | AXA FRANCE | AXA – NOTHING STOPS WOMEN'S RUGBY | Film | `financial_services` | “Nothing Stops Women’s Rugby” is a high-energy comedy film launched during the Six Nations that builds contrast between the absurd… |
| cannes_0014 | ANDREX | FIRST SCHOOL POO | Film | `fmcg` | Shockingly, 76% of UK school children now hold their poo at school. That’s 2.59 million secondary school-age children embarrassed,… |
| cannes_0015 | APPLE | SLIDE | Film | `technology` | "Slide" ran as a piece of film online, and on broadcast and streaming TV. It is part of the long running Relax its iPhone campaign… |
| cannes_0016 | ELI LILLY | NEVER OVER | Film | `healthcare` | For the Olympics, we took a perceived flaw - disciplined, methodical, repetitive process - and turned it into an anthemic rallying… |
| cannes_0017 | LILLY | GOOD DAYS | Film | `healthcare` | At the heart of this film is a love story. A love story tested, but never broken by the impact of Alzheimer’s. The film portrays t… |
| cannes_0018 | JOHN LEWIS & PARTNERS | TABLEAU | Film | `retail` | Film was the only medium capable of capturing a century of British life and John Lewis’ place within it. Tableau is a 100-second f… |
| cannes_0019 | INTERMARCHÉ | UNLOVED | Film | `retail` | This commercial is a 2’30 film that was broadcast on TV, cinemas and social media. For ten years, while competitors fought over pr… |
| cannes_0020 | AMAZON | BRING A BOOK TO LIFE | Film | `retail` | This work is relevant to film as it shows how the idea that stories only exist when someone reads them can be brought to life thro… |
| cannes_0021 | LA UNION NEWSPAPER AND ARTICLE 19 | BULLET MACHINE | Film | `media_entertainment` | The tension at the heart of Bullet Machine, a keystroke that triggers a bullet, only works as a sensory experience. You have to he… |
| cannes_0022 | OPEN AI | DISH | Film | `technology` | This film idea launched ChatGPT globally for the first time, with a concept that transformed life’s smallest moments into the bigg… |
| cannes_0023 | OPEN AI | PULL UP | Film | `technology` | This film idea launched ChatGPT globally for the first time, with a concept that transformed life’s smallest moments into the bigg… |
| cannes_0024 | OPEN AI | TRIP | Film | `technology` | This film idea launched ChatGPT globally for the first time, with a concept that transformed life’s smallest moments into the bigg… |
| cannes_0025 | DOVE | REAL BEAUTY: HOW A SOAP BRAND CREATED A  | Creative Strategy | `fashion_beauty` | Without creative strategy, Dove might still be a soap company. Instead, Dove is a $7.5bn social movement. In 2004, Dove launched t… |
| cannes_0026 | ASUNIWA | SATO 2531 | Creative Strategy | `charity` | SATO2531 illustrates the power of data and creativity to challenge ingrained attitudes, behaviors, even laws. Japan is the only co… |
| cannes_0027 | PROCOLOMBIA | HUMANIMAL TOURISM | Creative Strategy | `travel` | Every year, thousands of animals choose Colombia as their destination. What if humans followed their instincts too? We discovered … |
| cannes_0028 | AXA | AXA - THREE WORDS | Creative Strategy | `financial_services` | Three Words is at its core a strategic idea that reinvents home insurance contract as a tool to offer a way out for victims of dom… |
| cannes_0029 | ANNAHAR NEWSPAPER | THE NEW PRESIDENT | Creative Strategy | `media_entertainment` | AnNahar’s AI President campaign was not just an ad—it was a strategic intervention in Lebanon’s political crisis. With no elected … |
| cannes_0030 | PEDIGREE | PEDIGREE CARAMELO | Creative Strategy | `fmcg` ⚑ | This work shows how a culturally rooted strategy can drive global brand transformation. In Brazil, the most loved dog — the Carame… |
| cannes_0031 | APPLE | SHOT ON IPHONE | Creative Effectiveness | `technology` | By 2015, in the smartphone category, camera was everything. It had become the top for purchase consideration amongst all smartphon… |
| cannes_0032 | XBOX | THE EVERYDAY TACTICIAN: HOW GETTING A FO | Creative Effectiveness | `media_entertainment` | Launched in 1992, FM is one of the biggest-selling PC games ever. Xbox first brought FM to XGP in 2022, hoping to convert its 6Mn … |
| cannes_0033 | HERTOG JAN | DON'T DRINK HERTOG JAN | Creative Effectiveness | `alcohol` | Context Hertog Jan, a beloved Dutch craft beer brand, faced market dominance by larger competitors like Heineken. The concept of a… |
| cannes_0034 | SPECSAVERS | THE MISHEARD VERSION | Creative Effectiveness | `healthcare` ⚑ | Strategic Insight INSIGHT: HEARING LOSS IS SCARY BUT MISHEARING CONNECTS Our breakthrough insight was that "Hearing loss is isolat… |
| cannes_0035 | AXA | AXA - THREE WORDS | Direct | `financial_services` | With Three Words, we reprogrammed the most common insurance contract in France into a tool for escaping domestic violence. It trig… |
| cannes_0036 | MERCADO LIVRE | COUPON RAIN | Direct | `retail` | Coupon Rain leveraged a highly specific cultural moment in football to connect with fans of the sport like never before. By embedd… |
| cannes_0037 | IKEA | U UP? | Direct | `retail` | IKEA's 'U Up?' is relevant for Direct because it went straight to the audience most receptive to our mattress message: sleepless C… |
| cannes_0038 | PENNY | PRICE PACKS | Direct | `retail` | Price Packs is the first brand that's all about the price because the packaging design focuses entirely on it. It’s the ultimate c… |
| cannes_0039 | UBER EATS | FOOTBALL IS FOR FOOD | Direct | `food_drink` ⚑ | A NEW SALES ECOSYSTEM FOR FOOTBALL FANS, INSPIRED BY THE HIDDEN TRUTH BEHIND AMERICA’S FAVORITE SPORT Uber Eats transformed its NF… |
| cannes_0040 | LIDL | LIDLIZE | Direct | `retail` | Lidlize created an immediate and personalized connection between the brand and its consumers. Through AI technology, Lidl offered … |
| cannes_0041 | KFC | PRIZE ON THE BONE | Direct | `food_drink` | “Prize on the Bone” is a powerful Direct entry as it reignited consumer action through a culturally resonant mechanic uniquely rel… |
| cannes_0042 | INDIAN RAILWAYS | LUCKY YATRA | Direct | `travel` ⚑ | Lucky Yatra turned one of Indian Railways’ biggest challenges, ticketless riders, into its most valuable new audience. Using the i… |
| cannes_0043 | IKEA | IKEA HIDDEN TAGS | Direct | `retail` | The HIDDEN TAGS campaign drove an immediate and measurable response from a specific audience, using a human insight to inspire act… |
| cannes_0044 | ANGEL SOFT | POTTY-TUNITIES | Direct | `fmcg` | Angel Soft’s “Potty-tunity” campaign did the unthinkable. When it launched at the Super Bowl, a day filled with 3+ hours of FOMO-i… |
| cannes_0045 | ZIPLOC | PRESERVED PROMOS | Direct | `fmcg` | Using data to target our groups was more important than usual because our audience, demographically, was so broad (Gen Z all the w… |
| cannes_0046 | INDIAN RAILWAYS | LUCKY YATRA | PR | `travel` ⚑ | Lucky Yatra turned a financial liability into a national story of optimism and opportunity. By reimagining every Indian Railways t… |
| cannes_0047 | NORDEA | THE PARENTAL LEAVE MORTGAGE | PR | `financial_services` | The Parental Leave Mortgage turned a social insight into a national conversation—without a traditional campaign. By launching a re… |
| cannes_0048 | INDIAN RAILWAYS | LUCKY YATRA | PR | `travel` ⚑ | Lucky Yatra turned a financial liability into a national story of optimism and opportunity. By reimagining every Indian Railways t… |
| cannes_0049 | CLASH OF CLANS | HAALAND PAYBACK TIME | PR | `media_entertainment` | By uniting Haaland’s fans and haters, "Payback Time" generated widespread media and social buzz, fueled by the fierce rivalries of… |
| cannes_0050 | NUTTER BUTTER | NUTTER BUTTER, YOU GOOD? | PR | `food_drink` | The Nutterverse earned coverage and conversation for a previously invisible brand. Without any news to break, we still had to intr… |
| cannes_0051 | AXA | AXA - THREE WORDS | PR | `financial_services` | Home is the most dangerous place for women, according to the UN. This is where 1 in 5 women will experience domestic violence duri… |
| cannes_0052 | PROGRESSO | PROGRESSO SOUP DROPS | PR | `food_drink` | Driving awareness of Progresso soup during sick season (our brand’s biggest sales window) was no small task, with our competitive … |
| cannes_0053 | CORONA | SUN RESERVE | PR | `alcohol` | Through strategic and creative communication, Sun Reserve uses storytelling to address a pressing environmental issue while enhanc… |
| cannes_0054 | O2 | DAISY VS SCAMMERS | PR | `telecoms` | Daisy vs Scammers reframes O2 from victim to hero by chaining multiple AI systems to create a chatty AI granny that scams the scam… |
| cannes_0055 | DOVE | DOVE REAL BEAUTY REDEFINED FOR THE AI ER | Media | `fashion_beauty` | Our Media strategy reinforced Dove’s mission to reflect the beauty of inclusivity and authenticity amongst real women. To honor Do… |
| cannes_0056 | SKOL | RETRO INFLUENCERS | Media | `alcohol` | RetroInfluencers started with a media insight and became a full media experience. It began by reconnecting with our audience insid… |
| cannes_0057 | HEINZ KETCHUP & MUSTARD | CAN'T UNSEE IT | Media | `food_drink` | We didn’t simply launch this campaign with a traditional approach to media. We hacked the “Deadpool & Wolverine” marketing campaig… |
| cannes_0058 | HBO | “RAISE YOUR BANNERS” | Media | `media_entertainment` | We built this campaign by creating outdoor media canvases where they never existed, at an unprecedented scale. This executional co… |
| cannes_0059 | ROCKET | FIRST EVER LIVE COMMERCIAL CROSSOVER | Media | `financial_services` | This country was built on a powerful promise: work hard, and you could own a piece of it — a home of your own, a future to build o… |
| cannes_0060 | MERCADO LIVRE | COUPON RAIN | Media | `retail` | Coupon Rain used media in a strategic and unconventional way, starting with the trophy lift — a moment not traditionally seen as m… |
| cannes_0061 | KITKAT | STREET | Outdoor | `food_drink` | This campaign was made for the street. We placed our print ads in the exact environments where phone-induced detachment is most vi… |
| cannes_0062 | KITKAT | PUBLIC TRANSPORT | Outdoor | `food_drink` | This campaign was made for the street. We placed our print ads in the exact environments where phone-induced detachment is most vi… |
| cannes_0063 | KITKAT | CROWD | Outdoor | `food_drink` | This campaign was made for the street. We placed our print ads in the exact environments where phone-induced detachment is most vi… |
| cannes_0064 | PARIS 2024 | OLYMPIC GAMES OPENING CEREMONY PARIS 202 | Outdoor | `media_entertainment` ⚑ | The Paris 2024 Opening Ceremony redefined what outdoor experiences can achieve by transforming 6 kilometers of the Seine into the … |
| cannes_0065 | ITV X CALM | MISSED BIRTHDAYS | Outdoor | `charity` | Cannes’ outdoor track celebrates work that engages in the field and leverages public space to communicate a message. This entry sh… |
| cannes_0066 | PENNY | PRICE PACKS | Outdoor | `retail` | This idea is relevant for Outdoor because all relevant information is contained in these products, which are designed for maximum … |
| cannes_0067 | AXE/LYNX | SCRATCH & SNIFF | Outdoor | `fashion_beauty` | Outdoor has always been a powerful medium for showcasing beauty and design, but conveying scent has remained a challenge. AXE/LYNX… |
| cannes_0068 | HEINZ | TOAST | Outdoor | `food_drink` | With no brand, logo or pack, Heinz’s simple and bold ‘It Has to Be’ ads break the conventions of out-of-home. Each execution is co… |
| cannes_0069 | INDIAN RAILWAYS | LUCKY YATRA | Outdoor | `travel` ⚑ | Lucky Yatra transformed an everyday public touchpoint, the Indian Railways ticket, into a dynamic out-of-home brand experience. By… |
| cannes_0070 | STELLA ARTOIS | PROTECTOR OF THE CHALICE | Outdoor | `alcohol` | This campaign didn’t just work in outdoor, it relied on it. Placed near packed pubs in cities where space is scarce, each ad becam… |
| cannes_0071 | HEINZ | CHIPS | Outdoor | `food_drink` | With no brand, logo or pack, Heinz’s simple and bold ‘Trigger the Taste’ ads break the conventions of out-of-home. Each execution … |
| cannes_0072 | COCA-COLA | SHADES OF RED | Outdoor | `food_drink` | Decades in Mexico have made Coca-Cola more than just an everyday drink—it’s a cultural icon. And you can see it on every street: g… |
| cannes_0073 | RIMAS MUSIC | TRACKING BAD BUNNY | Outdoor | `media_entertainment` | Instead of a traditional paid outdoor placement, this campaign used Puerto Rico itself to reveal the tracklist of Bad Bunny’s high… |

⚑ = listed in the review table above.

## How to regenerate

The table is derived from the corpus and `normalise.CLIENT_TO_CATEGORY`. Change the
table, re-run the generator in this file's git history, and re-run:

```
cd engine/rag && python3 rag.py retag --corpus <corpus>/rag --index ./_index_v3 --apply
```

`retag` rewrites metadata on the existing index without re-embedding, because category
is filtered on, never embedded.
