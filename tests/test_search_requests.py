import unittest
from manager.discord.search_requests import search_request


class SearchRequestTests(unittest.TestCase):
    def test_common_verbs_preserve_title_and_explicit_criteria(self):
        title = 'Les Anneaux de pouvoir S01 en 2160p multi'
        for verb in ('cherche', 'recherche', 'ajoute', 'trouve', 'télécharge', 'dl', 'mets-moi', 'met moi', 'tu peux me trouver', 'pourrais-tu ajouter', 'tu pourrais nous chercher'):
            with self.subTest(verb=verb):
                self.assertEqual(search_request(verb + ' ' + title), title)

    def test_other_actions_and_negations_keep_normal_routing(self):
        for text in ('pourquoi mon téléchargement est bloqué ?', 'ne cherche pas Show', 'ajoute pas Show', 'cherche mon dernier téléchargement', 'téléchargement bloqué', 'tu peux expliquer le problème ?', 'bonjour', 'recherches récentes'):
            with self.subTest(text=text):
                self.assertIsNone(search_request(text))

    def test_empty_request_can_open_the_correction_form(self):
        self.assertEqual(search_request('cherche'), '')
        self.assertEqual(search_request('Ajoute : «Les Anneaux de pouvoir»'), 'Les Anneaux de pouvoir')
