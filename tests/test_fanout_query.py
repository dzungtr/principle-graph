import json
import unittest
from principle_graph.fanout import query_directions, render_markdown
from principle_graph.resolution import Entity
from principle_graph.review import GraphEdge

class Graph:
    def __init__(self):
        self.es = [Entity('e1','Rates','topic',('interest rates',)), Entity('e2','Stocks','topic'), Entity('e3','Bonds','topic')]
        self.edges = [GraphEdge('Rates','increases','Stocks',.8,'book:1',('rates rise',),'US'), GraphEdge('Bonds','hedges','Rates',.9,'book:2',('hedge',),'EU')]
    def entities(self): return self.es
    def edges_for(self, entity): return self.edges

class FanoutTests(unittest.TestCase):
    def test_exact_seed_assembles_incoming_and_outgoing_ranked(self):
        seeds, directions = query_directions('interest rates', Graph())
        self.assertEqual([d.relation for d in directions], ['HEDGES','INCREASES'])
        self.assertEqual(directions[0].neighbor, 'Bonds')
        self.assertEqual(directions[1].scope_conditions, 'US')
    def test_top_k_and_invalid_options(self):
        with self.assertRaises(ValueError): query_directions('Rates', Graph(), top_k=0)
        _, directions = query_directions('Rates', Graph(), top_k=1)
        self.assertEqual(len(directions), 1)
    def test_no_seed_rendering(self):
        seeds, directions = query_directions('unknown', Graph())
        self.assertEqual(render_markdown('unknown', seeds, directions), 'No matching seeds for: unknown')
    def test_json_shape_is_serializable(self):
        seeds, directions = query_directions('Rates', Graph())
        payload = {'query':'Rates', 'seeds':[{'name':s.entity.name,'score':s.score} for s in seeds],
                   'directions':[d.__dict__ for d in directions]}
        self.assertIn('directions', json.loads(json.dumps(payload)))

if __name__ == '__main__': unittest.main()
