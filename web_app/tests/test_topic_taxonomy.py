import unittest

from web_app.topic_taxonomy import classify_topic


class TopicTaxonomyTests(unittest.TestCase):
    def test_door_wording_variants_share_dimension_aspect(self):
        left = classify_topic("Provide the proposed door width", "Building")
        right = classify_topic("The 2'-6\" opening does not provide required clear width", "Building")
        self.assertEqual(left["topic_id"], "DOORS_DOOR_DIMENSIONS_AND_CLEAR_WIDTH")
        self.assertEqual(left["topic_id"], right["topic_id"])

    def test_tree_aspects_are_not_collapsed(self):
        protection = classify_topic("Add protective fencing and root protection around tree 4", "Arborist")
        report = classify_topic("Submit the project arborist report", "Arborist")
        self.assertNotEqual(protection["topic_id"], report["topic_id"])

    def test_fire_door_is_not_door_dimensions(self):
        result = classify_topic("Provide a fire-rated door assembly", "Building")
        self.assertEqual(result["topic_id"], "DOORS_DOOR_FIRE_RATING")


if __name__ == "__main__":
    unittest.main()
