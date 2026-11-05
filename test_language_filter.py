import unittest
from language_filter import analyze

BAD = [
    'blyаt','blуat','пuздa','блять','бля','сука','sosi','s0s1','s o s i','cоcи','с0си','6лять','6л9ть',
    'b.l.y.a.t','blyat','bl9t','nahuy','na hui','п1зд3ц','pizdec','пизdец',
    'xуй','xyй','х у й','п-и-з-д-е-ц','п***и***з***д***е***ц','ты дeбил',
    'ты loh','ты клоун','ti idiot','иди нахуй','я ебал','е6ать','ебаааать',
    'ты тупой','отсоси','чм0','f.u.c.k','5uk4','суkа','xуeсос','ты идиот',
    'пошёл нахер','ты пeтyx','идu нaхyй','п\u200bи\u200dздец','ＦＵＣＫ',
    'бляяяяять','сссууукккааа','пиздёж','заебись','заебал','пидорас',
    'тебе говорят, ты полный идиот','ты дурак, это слово «блять»',
    'слово «идиот» — это ты','слово «sosi», иди нахуй',
]
CLEAN = [
    'Доброе утро','сосиски','сосед','сосна','насос','особенности',
    'команда','мандарины','употребление','учеба','Херсон',
    'страхуй машину','подстрахуй меня','член команды',
    'вагина — анатомический термин','во дворе петух','мусор нужно вынести',
    'это тупой нож','я идиот','я такой дурак','ты не идиот',
    'sosiski','subscribe','class assignment','Scunthorpe',
    'соси леденец','сосать сок через трубочку',
    'это слово «блять» означает ругательство',
    'Что означает слово "sosi"?',
    'Он написал «иди нахуй», это оскорбление',
    'слово «идиот» — оскорбление','роман Идиот Достоевского',
    'ты прочитал роман «Идиот»?',
    'бляха на ремне','рубль','сук дерева','психолог','психиатр',
    'Я прошу не писать слово «сука» в чате',
]

class LanguageTests(unittest.TestCase):
    def test_obfuscations_and_abuse(self):
        for text in BAD:
            with self.subTest(text=text):self.assertTrue(analyze(text).offensive)
    def test_normal_words_and_context(self):
        for text in CLEAN:
            with self.subTest(text=text):self.assertFalse(analyze(text).offensive)
    def test_reply_does_not_make_neutral_sentence_an_insult(self):
        self.assertFalse(analyze('во дворе петух',addressed=True).offensive)
        self.assertTrue(analyze('клоун',addressed=True).offensive)
        self.assertFalse(analyze('клоун',addressed=False).offensive)
    def test_categories(self):
        self.assertEqual(analyze('ты идиот').category,'insult')
        self.assertEqual(analyze('блять, автобус уехал').category,'profanity')
        self.assertEqual(analyze('Что означает слово «блять»?').category,'discussion')
    def test_distant_target_does_not_poison_normal_noun(self):
        self.assertFalse(analyze('ты знаешь историю про ферму где по утрам очень громко кричит петух').offensive)
    def test_no_message_wide_word_concatenation(self):
        self.assertFalse(analyze('class is interesting').offensive)
        self.assertFalse(analyze('люди обсуждают Херсон и учебу').offensive)

if __name__=='__main__':unittest.main()
