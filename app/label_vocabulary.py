"""One vocabulary for the object detector and the vision model.

The object detector (ONNX, COCO classes) says ``cat``, ``dog``, ``car``,
``person``; the vision model writes what it sees in plain words: "kitten",
"puppy", "sedan", "courier". Without a bridge the two never agree: a tag
"kitten" cannot confirm a ``cat`` detection, and the label filter lists Cat
and "Kitten (AI tag)" side by side.

:func:`canonical_label` maps the model's words onto the detector class they
name, when there is one. AI tags are stored under the detector's word (so
they merge with detections and confirm or contradict them), while AI tag
alert rules still see the model's own words as well (see
app.ai_verification._description_record).
"""

from __future__ import annotations

# The detector classes the vision model's words map onto (COCO), with the
# everyday words, breeds and kinds that name them. Kept to unambiguous words:
# "vehicle" or "animal" name no single class and stay as they are.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    'person': (
        'people', 'man', 'men', 'woman', 'women', 'child', 'children', 'kid', 'kids', 'boy', 'girl',
        'adult', 'teenager', 'pedestrian', 'courier', 'delivery driver', 'delivery person',
        'postman', 'postwoman', 'mail carrier', 'worker', 'workman', 'tradesman', 'visitor', 'intruder',
        'resident', 'neighbour', 'neighbor', 'jogger', 'runner', 'walker', 'cyclist', 'rider',
    ),
    'cat': ('kitten', 'kitty', 'tabby', 'tabby cat', 'house cat', 'feline'),
    'dog': ('puppy', 'pup', 'canine', 'hound', 'labrador', 'retriever', 'terrier', 'spaniel',
            'german shepherd', 'border collie', 'collie', 'poodle', 'bulldog', 'beagle', 'kelpie'),
    'bird': ('magpie', 'crow', 'raven', 'pigeon', 'dove', 'seagull', 'gull', 'sparrow', 'myna',
             'cockatoo', 'galah', 'parrot', 'duck', 'chicken', 'hen', 'rooster', 'ibis', 'kookaburra',
             'owl', 'hawk', 'eagle', 'robin', 'blackbird', 'starling', 'lorikeet'),
    'horse': ('pony', 'foal', 'mare', 'stallion'),
    'sheep': ('lamb', 'ewe', 'ram'),
    'cow': ('cattle', 'calf', 'bull', 'steer', 'heifer'),
    'bear': ('grizzly', 'black bear', 'brown bear'),
    'car': ('sedan', 'hatchback', 'suv', 'coupe', 'convertible', 'wagon', 'station wagon',
            'taxi', 'cab', 'police car', 'sports car', 'automobile'),
    'truck': ('pickup', 'pickup truck', 'ute', 'lorry', 'tow truck', 'garbage truck', 'rubbish truck',
              'delivery truck', 'semi', 'semi-trailer', 'dump truck'),
    'bus': ('coach', 'minibus', 'school bus'),
    'motorcycle': ('motorbike', 'motor bike', 'scooter', 'moped', 'dirt bike'),
    'bicycle': ('bike', 'push bike', 'pushbike', 'e-bike', 'ebike', 'mountain bike'),
    'boat': ('dinghy', 'kayak', 'canoe', 'yacht'),
    'umbrella': ('parasol',),
    'backpack': ('rucksack',),
    'handbag': ('purse',),
    'suitcase': ('luggage',),
}

_CANONICAL: dict[str, str] = {}
for _class, _words in _SYNONYMS.items():
    _CANONICAL[_class] = _class
    for _word in _words:
        _CANONICAL[_word] = _class

# Coarse families for telling a contradiction ("a cat, not a dog") from two
# unrelated objects in one picture ("a dog and a car").
_FAMILIES: dict[str, str] = {
    **{name: 'animal' for name in ('cat', 'dog', 'bird', 'horse', 'sheep', 'cow', 'bear')},
    **{name: 'vehicle' for name in ('car', 'truck', 'bus', 'motorcycle', 'bicycle')},
    'person': 'person',
}


def _normalized(word: str) -> str:
    text = ' '.join(str(word or '').strip().lower().replace('_', ' ').split())
    # A simple plural ("cats", "kittens") names the same class.
    if text not in _CANONICAL and text.endswith('s') and text[:-1] in _CANONICAL:
        return text[:-1]
    return text


def canonical_label(word: str) -> str | None:
    """The detector class ``word`` names (``kitten`` -> ``cat``), or None."""
    return _CANONICAL.get(_normalized(word))


def canonical_tag(word: str) -> str:
    """``word`` in the detector's vocabulary when it names a class, else as written."""
    return canonical_label(word) or str(word or '').strip().lower()


def family(label: str) -> str | None:
    """``animal``, ``vehicle`` or ``person`` for a detector class, else None."""
    return _FAMILIES.get(canonical_label(label) or '')
