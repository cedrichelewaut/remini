#!/usr/bin/env python3
"""Classify non-English reviews into the same feature-request taxonomy used
for English, without translation - each language gets its own request-signal
and category-keyword patterns, hand-written (not machine translated).

Reads all_reviews from a reviews_output.json (produced by extract_reviews.py),
detects each review's language with py3langid, and classifies the non-English
ones. English reviews are skipped here - extract_reviews.py already covers
those.

Usage:
    python classify_multilingual.py --in reviews_output.json --out multilingual_findings.json
"""
import argparse
import json
import re
from collections import defaultdict

import py3langid as langid

# Generic "please / I wish / it would be nice / should" equivalents, per
# language. Deliberately broad, same spirit as the English signal list.
REQUEST_SIGNALS = {
    "es": [r"por favor", r"ojal[aá]", r"me gustar[ií]a que", r"ser[ií]a (bueno|genial|bonito)",
           r"deber[ií]an?", r"necesita(n)?", r"quisiera que", r"espero que", r"falta (un|una)",
           r"a[nñ]adan", r"agreguen", r"opci[oó]n de", r"deber[ií]a (haber|tener)"],
    "pt": [r"por favor", r"seria (bom|[oó]timo|legal)", r"gostaria que", r"poderiam",
           r"deveria(m)?", r"espero que", r"falta (um|uma)", r"adicionem", r"op[cç][aã]o de"],
    "fr": [r"s'il (vous|te) pla[iî]t", r"j'aimerais que", r"ce serait (bien|cool|super) si",
           r"il faudrait", r"pourriez[- ]vous", r"j'esp[eè]re que", r"manque (un|une)",
           r"ajoutez", r"option (pour|de)", r"devrait (avoir|y avoir)"],
    "it": [r"per favore", r"sarebbe bello se", r"vorrei che", r"dovrebbero", r"spero che",
           r"manca (un|una)", r"aggiung(a|ete)", r"opzione (per|di)"],
    "de": [r"\bbitte\b", r"w[aä]re (sch[oö]n|toll|gut) wenn", r"ich w[uü]nsche mir",
           r"sollte(n)?", r"hoffe(ntlich)?", r"fehlt (ein|eine)", r"f[uü]g(t|en) .* hinzu",
           r"option (f[uü]r|zu)"],
    "id": [r"\btolong\b", r"\bsemoga\b", r"seharusnya", r"\bmohon\b", r"seandainya",
           r"harap(kan)?", r"kurang(nya)? (ada|fitur)", r"tambahkan"],
    "pl": [r"prosz[eę]", r"by[lł]oby (mi[lł]o|super|dobrze) gdyby", r"chcia[lł]bym [zż]eby",
           r"powinni", r"mam nadziej[eę]", r"brakuje", r"dodajcie"],
    "nl": [r"zou (fijn|leuk|goed) zijn als", r"graag zou ik", r"zouden? moeten",
           r"hopelijk", r"mis (een|ik)", r"voeg .* toe", r"alsjeblieft"],
    "tr": [r"l[uü]tfen", r"ke[sş]ke", r"umar[iı]m", r"eklen(meli|se)",
           r"olsa (iyi|g[uü]zel) olur", r"ekleyin", r"eksik"],
    "vi": [r"l[aà]m [ơo]n", r"mong (mu[oố]n|r[aằ]ng)", r"n[eê]n c[oó]", r"hy v[oọ]ng",
           r"th[eê]m (t[ií]nh n[aă]ng|ch[uứ]c n[aă]ng)", r"thi[eế]u"],
    "ja": [r"追加してほしい", r"お願いします", r"改善してほしい", r"してくれたら嬉しい",
           r"欲しいです", r"できるようにしてほしい"],
    "ko": [r"추가해주세요", r"좋겠어요", r"개선해주세요", r"바랍니다", r"있었으면", r"해주시면"],
    "th": [r"อยากให้เพิ่ม", r"กรุณาเพิ่ม", r"น่าจะมี", r"หวังว่า", r"ขอให้"],
    "ru": [r"пожалуйста", r"было бы (хорошо|здорово) если", r"хотелось бы",
           r"добавьте", r"не хватает"],
}

# Category keywords per language, for the highest-value categories only
# (the ones that showed real volume/signal in English). Not exhaustive -
# anything that doesn't match a category still gets flagged as "other" if
# it matched a request signal, so nothing is silently dropped.
CATEGORY_KEYWORDS = {
    "pricing / subscription": {
        "es": [r"suscripci[oó]n", r"\bprecio\b", r"\bcaro\b", r"prueba gratis", r"reembolso"],
        "pt": [r"assinatura", r"\bpre[cç]o\b", r"\bcaro\b", r"teste gr[aá]tis", r"reembolso"],
        "fr": [r"abonnement", r"\bprix\b", r"\bcher\b", r"essai gratuit", r"remboursement"],
        "it": [r"abbonamento", r"\bprezzo\b", r"\bcaro\b", r"prova gratuita", r"rimborso"],
        "de": [r"\babo\b", r"abonnement", r"\bpreis\b", r"\bteuer\b", r"kostenlose testversion",
               r"r[uü]ckerstattung"],
        "id": [r"langganan", r"\bharga\b", r"\bmahal\b", r"uji coba gratis", r"pengembalian dana"],
        "pl": [r"subskrypcj\w*", r"\bcena\b", r"\bdrogi\b", r"darmowy okres pr[oó]bny", r"zwrot"],
        "nl": [r"abonnement", r"\bprijs\b", r"\bduur\b", r"gratis proefperiode", r"terugbetaling"],
        "tr": [r"abonelik", r"\bfiyat\b", r"pahal[iı]", r"[uü]cretsiz deneme", r"\biade\b"],
        "vi": [r"đăng k[yý]", r"\bgi[aá]\b", r"\bđắt\b", r"d[uù]ng th[uử] mi[eễ]n ph[ií]", r"ho[aà]n ti[eề]n"],
    },
    "ads": {
        "es": [r"anuncio"], "pt": [r"an[uú]ncio"], "fr": [r"publicit[eé]"],
        "it": [r"pubblicit[aà]"], "de": [r"werbung"], "id": [r"\biklan\b"],
        "pl": [r"reklam\w*"], "nl": [r"advertentie"], "tr": [r"reklam"], "vi": [r"quảng c[aá]o"],
    },
    "improving AI model / output quality": {
        "es": [r"poco realista", r"\bartificial\b", r"distorsi[oó]n", r"no se parece a m[ií]"],
        "pt": [r"\birreal\b", r"\bartificial\b", r"distor[cç][aã]o", r"n[aã]o se parece comigo"],
        "fr": [r"irr[eé]aliste", r"artificiel", r"d[eé]form[eé]", r"ne me ressemble pas"],
        "it": [r"irrealistico", r"artificiale", r"distort[oa]", r"non mi somiglia"],
        "de": [r"unrealistisch", r"k[uü]nstlich", r"verzerrt", r"sieht nicht (wie ich|aus wie ich)",
               r"andere person"],
        "id": [r"tidak realistis", r"tidak alami", r"distorsi"],
        "tr": [r"ger[cç]ek[cç]i de[gğ]il", r"\byapay\b", r"\bbozuk\b"],
        "nl": [r"onrealistisch", r"kunstmatig", r"vervormd"],
    },
    "batch / bulk / queue processing": {
        "es": [r"por lotes", r"varias fotos a la vez", r"\bcola\b"],
        "pt": [r"em lote", r"v[aá]rias fotos de uma vez", r"\bfila\b"],
        "fr": [r"en lot", r"plusieurs photos [aà] la fois", r"file d'attente"],
        "de": [r"\bstapel\b", r"mehrere fotos gleichzeitig", r"warteschlange"],
    },
    "usage limits / free tier": {
        "es": [r"l[ií]mite (de|diario)", r"fotos gratis al d[ií]a"],
        "pt": [r"limite (de|di[aá]rio)", r"fotos gr[aá]tis por dia"],
        "fr": [r"limite (de|quotidienne)", r"photos gratuites par jour"],
        "de": [r"\blimit\b", r"kostenlose fotos pro tag"],
        "tr": [r"g[uü]nl[uü]k (limit|s[iı]n[iı]r)", r"[uü]cretsiz foto"],
    },
    # The priority segment - couples / multiple people, generation feature specifically.
    "AI photo generation: couples / multi-person": {
        "es": [r"pareja", r"generador de pareja", r"fotos? de pareja"],
        "pt": [r"casal", r"gerador de casal", r"fotos? de casal"],
        "fr": [r"couple", r"g[eé]n[eé]rateur de couple", r"photos? de couple"],
        "it": [r"coppia", r"generatore di coppia", r"foto di coppia"],
        "de": [r"partnerbild", r"zu zweit generier", r"paarbild", r"partner.?foto"],
        "nl": [r"koppel ?foto"],
        "tr": [r"[cç]ift (foto[gğ]raf[iı]|olu[sş]turma)"],
    },
    "multi-person photo enhancement quality": {
        "es": [r"foto (de |en )?grupo", r"varias personas", r"misma cara"],
        "pt": [r"foto (de |em )?grupo", r"v[aá]rias pessoas", r"mesmo rosto"],
        "fr": [r"photo de groupe", r"plusieurs personnes", r"m[eê]me visage"],
        "de": [r"gruppenfoto", r"mehrere personen", r"gleiche(s)? gesicht"],
        "nl": [r"groepsfoto", r"groep van mensen", r"zelfde gezicht"],
    },
}


def classify(text, lang):
    if lang not in REQUEST_SIGNALS:
        return None  # no patterns for this language yet
    lower = text.lower()
    if not any(re.search(p, lower) for p in REQUEST_SIGNALS[lang]):
        return None
    matched = []
    for category, by_lang in CATEGORY_KEYWORDS.items():
        patterns = by_lang.get(lang)
        if patterns and any(re.search(p, lower) for p in patterns):
            matched.append(category)
    return matched or ["other / uncategorized"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", default="reviews_output.json")
    parser.add_argument("--out", default="multilingual_findings.json")
    args = parser.parse_args()

    data = json.load(open(args.input, encoding="utf-8"))
    reviews = data["all_reviews"]

    buckets = defaultdict(list)
    lang_counts = defaultdict(int)
    screened = 0
    for r in reviews:
        text = (r.get("text") or "").strip()
        if len(text) < 8:
            continue
        lang, _ = langid.classify(text)
        r["_lang"] = lang
        if lang == "en" or lang not in REQUEST_SIGNALS:
            continue
        screened += 1
        lang_counts[lang] += 1
        categories = classify(text, lang)
        if categories is None:
            continue
        for cat in categories:
            buckets[cat].append({**r, "matched_language": lang})

    print(f"Screened {screened} non-English reviews across {len(lang_counts)} languages")
    for lang, count in sorted(lang_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {lang}: {count}")

    print("\nMultilingual feature-request summary:")
    for cat, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {cat}: {len(items)}")

    json.dump(
        {"screened": screened, "lang_counts": lang_counts, "feature_requests": dict(buckets)},
        open(args.out, "w", encoding="utf-8"),
        indent=2,
        ensure_ascii=False,
    )
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
