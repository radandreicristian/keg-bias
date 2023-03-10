import datasets.arrow_dataset
import numpy as np
import scipy.signal
import spacy
import torch
import transformers
import typer
from datasets.load import load_dataset
import time
import pandas as pd
import asent  # noqa
from statistics import mean

from sklearn.metrics.pairwise import cosine_similarity as cos
from src.bookcorpus.seat_utils import run_test
from collections import defaultdict
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForMaskedLM
from src.similarity.cli import neg_samples, pos_samples, target_templates, encode
from src.utils.print import pretty_print

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

invalid_entities = ['he', 'she', 'his', 'hers', 'they', 'their', 'that\'s-']

transformers.logging.set_verbosity_error()
datasets.arrow_dataset.logging.set_verbosity_error()
datasets.logging.set_verbosity_error()
datasets.utils.disable_progress_bar()


def fill_templates(terms, templates):
    return [template.format(term) for term in terms for template in templates]


def get_language_pipeline():
    nlp = spacy.load("en_core_web_md")
    nlp.disable_pipes("tok2vec", "tagger", "parser", "attribute_ruler", "lemmatizer")
    nlp.add_pipe('sentencizer')
    nlp.add_pipe('asent_en_v1')
    return nlp


def load_bookcorpus_dataset(limit: int):
    return load_dataset("bookcorpus")["train"][:limit]


def create_dataframe():
    return pd.DataFrame(columns=['text', 'sentiment', 'entities'])

def divide_in_batches(dataset : list[str], batch_size : int = 1024):
    """
    Generator for dividing a dataset into batches
    """
    for i in range(0, len(dataset), batch_size):
        yield dataset[i:(i+batch_size)]

def experiment(limit: int = 1000):
    dataset = load_bookcorpus_dataset(limit)

    nlp = get_language_pipeline()

    # Initialize a DataFrame that stores rows from BookCorpus with entities
    entity_texts_df = create_dataframe()

    # For the first N rows in the training set, copy those that contain a person entity reference
    for text in tqdm(dataset['text']):
        doc = nlp(text)
        if doc.ents:
            if not any([ent.label_ == 'PERSON' for ent in doc.ents]):
                continue
            if all([ent.text in invalid_entities for ent in doc.ents]):
                continue
            sentiment = mean([dict(sentence._.polarity)['compound'] for sentence in doc.sents])
            entities = ' '.join([ent.text for ent in doc.ents])
            entry = pd.DataFrame([{"text": text, "sentiment": sentiment, "entities": entities}])

            entity_texts_df = pd.concat([entity_texts_df, entry], axis=0, ignore_index=True)

            # Do some kind of sentiment analysis?

    entity_texts_df.sort_values("sentiment", ascending=True, inplace=True)
    entity_texts_df.to_csv(f'entity_rows_{time.time()}.csv')


def experiment_sentiment(limit: int = 10000,
                         top_k: int = 15,
                         model_name='bert-large-cased',
                         n_processes: int = typer.Option(1, help="Number of processes to use for NER (-1 to use all cores)."),
                         batch_size: int = 16384):
    typer.echo(f"Loading model under bias investigation {model_name}...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForMaskedLM.from_pretrained(model_name).to(device)

    typer.echo("Loading dataset...")
    # Create a dictionary of shape {'entity_name': ['context_sentiment_scores']}

    # Extract the first [limit] rows from bookcorpus
    dataset = load_bookcorpus_dataset(limit)

    typer.echo("Loading language pipeline...")
    nlp = get_language_pipeline()
    typer.echo(f"   Active pipeline steps: {nlp.pipe_names}")

    entities_sentiment_scores = defaultdict(list)

    typer.echo("Processing data...")
    with tqdm(total=limit) as pbar:
        for dataset_batch in divide_in_batches(dataset['text'], batch_size=batch_size):
            doc_batch = nlp.pipe(dataset_batch, n_process=n_processes)
            for doc in doc_batch:
                entities = doc.ents
                # Check for names. If there is a name, calculate the sentiment score for the sentence
                for entity in entities:
                    if entity.label_ == 'PERSON' and entity.text not in invalid_entities and len(entity.text.split()) <= 1:
                        # Append the sentiment score to the value of the entity name in the dict.
                        sentiment = mean([dict(sentence._.polarity)['compound'] for sentence in doc.sents])
                        entities_sentiment_scores[entity.text].append(sentiment)
            pbar.update(len(dataset_batch))

    typer.echo("Computing average sentiment scores per entity...")

    # Compute mean/median of the context sentiment scores for entities with more than 1 appearance
    average_entity_sentiment_scores = {k: {"mean": mean(v),
                                           "len": len(v)} for k, v in entities_sentiment_scores.items() if len(v) > 1}

    # Take K entities with the highest score, K with the lowest
    sorted_average_entity_sentiment_scores = list(average_entity_sentiment_scores.items())

    sorted_average_entity_sentiment_scores.sort(key=lambda x: x[1]["mean"])

    top_positive = [k for k in sorted_average_entity_sentiment_scores[-top_k:]]
    top_negative = [k for k in sorted_average_entity_sentiment_scores[:top_k]]

    typer.echo(f"Top negatives: {', '.join([pretty_print(item) for item in top_negative])}")
    typer.echo(f"Top positive: {', '.join([pretty_print(item) for item in top_positive])}")

    top_positive_entities = [k[0] for k in top_positive]
    top_negative_entities = [k[0] for k in top_negative]

    # Create SEAT category templates using those names
    positive_concept_terms = fill_templates(top_positive_entities, target_templates)
    positive_concept_embeddings = np.array([encode(template, tokenizer, model) for template in positive_concept_terms])

    negative_concept_terms = fill_templates(top_negative_entities, target_templates)
    negative_concept_embeddings = np.array([encode(template, tokenizer, model) for template in negative_concept_terms])

    negative_samples_embeddings = np.array([encode(sample, tokenizer, model) for sample in neg_samples])

    positive_samples_embeddings = np.array([encode(sample, tokenizer, model) for sample in pos_samples])

    # Compute SEAT score and cohen's
    typer.echo("Running SEAT Test...")
    cohens_d, p_value = run_test(x=positive_concept_embeddings, y=negative_concept_embeddings,
                                 a=positive_samples_embeddings, b=negative_samples_embeddings,
                                 n_samples=10000, parametric=False)

    print(f"Effect size {cohens_d}, P-value: {p_value}")


if __name__ == '__main__':
    typer.run(experiment_sentiment)
