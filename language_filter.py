"""Deterministic Russian chat moderation. No API keys or external text uploads.

This is a contextual heuristic, not a general language model. It deliberately
keeps ambiguous ordinary words out of the unconditional profanity dictionary.
"""
from dataclasses import dataclass
import re
import unicodedata

@dataclass(frozen=True)
class Verdict:
    category: str
    reason: str = ''
    @property
    def offensive(self):
        return self.category in {'profanity', 'insult'}

# Entire words / morphological families, never arbitrary substring searches.
OBSCENE = re.compile(
    r'^(?:бля(?:д[а-я]*|т[а-я]*|)|сук(?:а|и|у|ой|ою|ам|ами|е|ою)|суч(?:ка|ки|ку|ье|ий|ья|онок|ара)[а-я]*|'
    r'(?:на|по|не|ни|до|за|о|а|в|вы|при|раз|рас|у|от)?ху(?:й|я|е|и|ю)[а-я]*|'
    r'(?:вз|на|по|о|а)?хуев[а-я]*|'
    r'(?:рас|раз|про|на|от|вы|за|до|под|при|с)?пизд[а-я]*|'
    r'(?:за|на|вы|у|от|под|пере|про|раз|до|при|об|в|с)?(?:ъ|ь)?(?:еб|йоб)(?:а|и|у|н|л|е|о|ы|т)[а-я]*|'
    r'долбо[а-я]*|хуесос[а-я]*|залуп[а-я]*|г[ао]ндон[а-я]*|'
    r'пид(?:ор|ар|р)[а-я]*|муд(?:ак|ил|озвон)[а-я]*|шлюх[а-я]*|шалав[а-я]*|'
    r'нахер[а-я]*|похер[а-я]*|херн[а-я]*|хер(?:а|ом|у)?|'
    r'fuck(?:ing|ed|er|ers|s)?|fck|shit(?:ty)?|bitch(?:es)?)$')
INSULT = re.compile(
    r'^(?:идиот[а-я]*|дебил[а-я]*|дурак(?:и|а|ом|у)?|дурочка|дура|'
    r'кретин[а-я]*|придур[а-я]*|ублюд[а-я]*|мраз[а-я]*|'
    r'чмо[а-я]*|чмыр[а-я]*|лох(?:и|а|ом|у)?|лошар[а-я]*|'
    r'тупиц[а-я]*|тупорыл[а-я]*|тупоголов[а-я]*|безмозгл[а-я]*|'
    r'ничтожество|сволоч[а-я]*|говноед[а-я]*)$')
AMBIGUOUS = re.compile(
    r'^(?:петух[а-я]*|козел|козлы|козлина|свинья|свиньи|свиньей|'
    r'скотина|животное|животные|осел|ослы|клоун[а-я]*|мусор|помойка|'
    r'тупой|тупая|тупые|тупое|глупый|глупая|псих|урод|уроды|'
    r'бездарь|лузер|неудачник|сосун|терпила|душнила|гад|позорище)$')
SEXUAL = re.compile(r'^(?:отсоси(?:те)?|отсосать|соси(?:те)?|сосать|сосал[аи]?)$')
LEET = str.maketrans({'0':'о','1':'и','3':'е','4':'ч','5':'с','6':'б','7':'т','8':'в','9':'я','@':'а','$':'с'})
LEET_LATIN = str.maketrans({'0':'o','1':'i','3':'e','4':'a','5':'s','6':'b','7':'t','8':'b','9':'ya','@':'a','$':'s'})
VISUAL = dict(zip('aceopxykmtbhi','асеорхукмтвни'))
VISUAL.update({'u':'и'})
CYR_LATIN = str.maketrans(dict(zip('абвгдезийклмнопрстуфхыь', ['a','b','v','g','d','e','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','y',''])))
CYR_VISUAL = str.maketrans({'а':'a','е':'e','о':'o','р':'p','с':'c','у':'y','х':'x','к':'k','м':'m','т':'t','в':'b','н':'h','и':'i'})
PHONETIC = dict(zip('abcdefghijklmnopqrstuvwxyz','абкдефгхийклмнопкрстуввхыз'))
PHONETIC.update({'c':'ц','q':'к','w':'в','j':'й','y':'й'})
DIGRAPHS = {'shch':'щ','sch':'щ','zh':'ж','kh':'х','ch':'ч','sh':'ш','ts':'ц','yu':'ю','ju':'ю','ya':'я','ja':'я','yo':'е','jo':'е','ye':'е'}
TOKEN = re.compile(r'[a-zа-яё0-9@$]+(?:[.*_\-|]+[a-zа-яё0-9@$]+)*', re.I)
QUOTES = re.compile(r'«[^»\n]{1,1000}»|"[^"\n]{1,1000}"|“[^”\n]{1,1000}”|‘[^’\n]{1,1000}’|\x27[^\x27\n]{1,1000}\x27')
DISCUSSION = re.compile(r'\b(?:слов[оа]|словом|выражени[ея]|термин|означает|значени[ея]|перевод|переводится|написани[ея]|употреблени[ея]|этимологи[яи]|цитат[ауы]|процитировал|обозвал|оскорбил|написал|запрещено|нельзя|не\s+пишите|не\s+говорите)\b', re.I)
TARGET = {'ты','тебя','тебе','тобой','вы','вас','вам','вами','иди','пошел','пошла','пошли','заткнись','заткнитесь','твой','твоя','ваш','ваша'}

def clean(text):
    text=unicodedata.normalize('NFKC',text).casefold().replace('ё','е')
    return ''.join(c for c in text if unicodedata.category(c)!='Cf')

def variants(token):
    raw=re.sub(r'[.*_\-|]','',clean(token))
    # Keep both e/з and и/л interpretations used in leetspeak.
    bases={raw,raw.translate(LEET),raw.translate(LEET_LATIN)}
    if '3' in raw:
        bases.add(raw.replace('3','з').translate(LEET))
    bases |= {base.translate(CYR_LATIN) for base in tuple(bases)} | {base.translate(CYR_VISUAL) for base in tuple(bases)}
    out=set()
    for base in bases:
        for visual in (False,True):
            for y in ('й','ы','у'):
                value=base
                if not visual:
                    for a,b in DIGRAPHS.items():value=value.replace(a,b)
                mapping={**PHONETIC,**(VISUAL if visual else {}),'y':y}
                value=''.join(mapping.get(c,c) for c in value)
                out.add(re.sub(r'(.)\1+',r'\1',value))
        out.add(re.sub(r'(.)\1+',r'\1',base))
    return out

def tokens(text):
    found=list(TOKEN.finditer(text))
    result=[(m.start(),m.end(),variants(m.group())) for m in found]
    # Recover only runs of single letters, not a compacted whole sentence.
    i=0
    while i<len(found):
        j=i
        while j<len(found) and len(found[j].group())==1:
            if j>i and (found[j].start()-found[j-1].end()>3 or re.search(r'[^\s.*_\-|]',text[found[j-1].end():found[j].start()])):break
            j+=1
        if j-i>=3:
            result.append((found[i].start(),found[j-1].end(),variants(''.join(m.group() for m in found[i:j]))))
        i=max(i+1,j)
    return sorted(result)

def analyze(text, *, addressed=False):
    """Inspect this message only. addressed means a reply to another user.

    Profanity is disallowed even without a target. Ordinary negative nouns need
    a nearby target; quoted word discussions may pass only if no abuse remains
    outside the quote. Replying does not itself make ordinary nouns abusive.
    """
    text=clean(text)
    items=tokens(text)
    quotes=list(QUOTES.finditer(text))
    unquoted=QUOTES.sub(' ',text)
    outside_words=set().union(*(forms for _,_,forms in tokens(unquoted))) if unquoted.strip() else set()
    discussion=bool(quotes and DISCUSSION.search(unquoted) and not (outside_words & TARGET) and not re.search(r'@[a-z0-9_]{3,}',unquoted))
    quoted_hits=False
    for index,(start,end,forms) in enumerate(items):
        near=items[max(0,index-3):index]+items[index+1:index+4]
        before=items[max(0,index-3):index]
        local=set().union(*(v for _,_,v in near)) if near else set()
        previous=set().union(*(v for _,_,v in before)) if before else set()
        explicit=bool(local & TARGET or re.search(r'@[a-z0-9_]{3,}',text[max(0,start-50):end+50]))
        strong=any(OBSCENE.fullmatch(w) for w in forms)
        insult=any(INSULT.fullmatch(w) for w in forms)
        ambiguous=any(AMBIGUOUS.fullmatch(w) for w in forms)
        sexual=any(SEXUAL.fullmatch(w) for w in forms)
        if sexual:
            literal=bool(local & {'леденец','леденцы','конфету','конфеты','сок','трубочку','палец','пальцы','грудь','молоко','соску','лед','карамель'})
            # Bare "соси"/sosi is commonly an insult; infinitives require a target.
            sexual=(not literal and (explicit or any(w in {'соси','сосите','отсоси','отсосите','отсосать'} for w in forms)))
        literary=('идиот' in forms and bool(local & {'роман','книга','достоевского','достоевский'}))
        # Negated and self-directed mild labels are not attacks on another user.
        negated=bool(before and 'не' in before[-1][2])
        self_label=bool(previous & {'я','сам','сама','мы'} and not explicit)
        attack=(insult or ambiguous) and not negated and not self_label and not literary and (explicit or insult or (ambiguous and addressed and len(items)==1))
        if not (strong or attack or sexual):continue
        in_quote=any(q.start()<=start and end<=q.end() for q in quotes)
        if discussion and in_quote:
            quoted_hits=True;continue
        return Verdict('profanity' if strong else 'insult','мат' if strong else 'оскорбление участника')
    return Verdict('discussion' if quoted_hits else 'clean')

def normalize(text):
    return ' '.join(sorted(forms)[0] for _,_,forms in tokens(clean(text)))
def is_offensive(text):return analyze(text).offensive
def is_directed_insult(text):return analyze(text).category=='insult'
def has_latin_profanity(text):return analyze(text).offensive
