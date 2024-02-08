import warnings
from collections import defaultdict
from pathlib import Path
from typing import Iterator, List, Literal, NamedTuple, Sequence

import ir_datasets
import pandas as pd
import torch
from lightning import LightningDataModule
from torch.utils.data import DataLoader, Dataset, IterableDataset
from transformers import AutoConfig

from tide.data import Batch, Sample
from tide.mvr import MVRConfig, MVRTokenizer

DASHED_DATASET_MAP = {
    dataset.replace("/", "-"): dataset for dataset in ir_datasets.registry._registered
}
RUN_HEADER = ["query_id", "q0", "doc_id", "rank", "score", "system"]


class IRDataset:
    def __init__(self, dataset: str) -> None:
        self.dataset = ir_datasets.load(dataset)
        self.queries = pd.DataFrame(self.dataset.queries_iter()).set_index("query_id")[
            "text"
        ]
        self.docs = self.dataset.docs_store()


class RunDatasetConfig(NamedTuple):
    targets: Literal["relevance", "rank"]
    depth: int
    sample_size: int
    sampling_strategy: Literal["single_relevant", "top"]


class RunDataset(IRDataset, Dataset):
    def __init__(
        self,
        run_dataset: Path,
        config: RunDatasetConfig,
    ) -> None:
        super().__init__(DASHED_DATASET_MAP[run_dataset.stem])
        self.run = pd.read_csv(
            run_dataset,
            sep=r"\s+",
            header=None,
            names=RUN_HEADER,
            usecols=[0, 2, 3, 4],
            dtype={"query_id": str, "doc_id": str},
        )
        self.config = config
        self.depth = config.depth
        if self.depth != -1:
            self.run = self.run[self.run["rank"] <= config.depth]

        self.qrels = pd.DataFrame(self.dataset.qrels_iter()).set_index(
            ["query_id", "doc_id"]
        )["relevance"]
        self.qrels = self.qrels.loc[
            pd.IndexSlice[self.run["query_id"].drop_duplicates(), :]
        ]
        self.run = self.run.merge(self.qrels, on=["query_id", "doc_id"], how="outer")
        self.run = self.run.sort_values(["query_id", "rank"])
        self.groups = self.run.groupby("query_id")
        self.query_ids = list(self.groups.groups.keys())

        if self.run["rank"].max() < config.depth:
            warnings.warn("Depth is greater than the maximum rank in the run file.")
        if config.sampling_strategy == "top" and config.sample_size > config.depth:
            warnings.warn(
                "Sample size is greater than depth and top sampling strategy is used. "
                "This can result in documents not contained in the run file, but that "
                "are present in the qrels, to be sampled."
            )

    def __len__(self) -> int:
        return len(self.query_ids)

    def __getitem__(self, idx: int) -> Sample:
        query_id = str(self.query_ids[idx])
        group = self.groups.get_group(query_id).copy()
        query = self.queries[query_id]
        if self.config.sampling_strategy == "single_relevant":
            relevant = group.loc[group["relevance"] > 0].sample(1)
            non_relevant_bool = (
                group["relevance"].fillna(0).eq(0) & ~group["rank"].isna()
            )
            num_non_relevant = non_relevant_bool.sum()
            sample_non_relevant = min(self.config.sample_size - 1, num_non_relevant)
            non_relevant = group.loc[non_relevant_bool].sample(sample_non_relevant)
            group = pd.concat([relevant, non_relevant])
            relevance = None
        else:
            relevance = tuple(group["relevance"].fillna(0))
            group = group.head(self.config.sample_size)

        doc_ids = tuple(group["doc_id"])
        docs = tuple(self.docs.get(doc_id).default_text() for doc_id in doc_ids)
        targets = tuple(
            group.set_index("doc_id").loc[list(doc_ids), self.config.targets].fillna(0)
        )
        return Sample(query_id, query, doc_ids, docs, targets, relevance)


class TriplesDataset(IRDataset, IterableDataset):
    def __init__(self, triples_dataset: str) -> None:
        super().__init__(triples_dataset)

    def __iter__(self) -> Iterator[Sample]:
        for doc in self.dataset.docpairs_iter():
            query_id = doc.query_id
            doc_id_a = doc.doc_id_a
            doc_id_b = doc.doc_id_b
            score_a = doc.score_a if hasattr(doc, "score_a") else 1
            score_b = doc.score_b if hasattr(doc, "score_b") else 0
            doc_a = self.docs.get(doc_id_a).default_text()
            doc_b = self.docs.get(doc_id_b).default_text()
            query = self.queries.loc[query_id]
            yield Sample(
                query_id,
                query,
                (doc_id_a, doc_id_b),
                (doc_a, doc_b),
                (score_a, score_b),
            )


class MVRDataModule(LightningDataModule):
    def __init__(
        self,
        model_name_or_path: str | Path,
        config: MVRConfig | None,
        num_workers: int,
        batch_size: int,
        train_dataset: str | None,
        inference_datasets: Sequence[str] | None = None,
        train_run_config: RunDatasetConfig | None = None,
        inference_run_config: RunDatasetConfig | None = None,
    ) -> None:
        super().__init__()
        self.config = MVRConfig.from_other(
            AutoConfig.from_pretrained(model_name_or_path)
        )
        if config is not None:
            self.config.update(config.to_mvr_dict())
        self.tokenizer = MVRTokenizer.from_pretrained(
            model_name_or_path, **self.config.to_tokenizer_dict()
        )
        self.max_length = self.config.max_position_embeddings
        self.num_workers = num_workers
        self.batch_size = batch_size

        self.train_dataset = train_dataset
        self.inference_datasets = inference_datasets
        self.train_run_config = train_run_config
        self.inference_run_config = inference_run_config

    def setup(self, stage: str) -> None:
        if stage == "fit":
            if self.train_dataset is None:
                raise ValueError("A training dataset must be provided.")
            if Path(self.train_dataset).suffix == ".run":
                if self.train_run_config is None:
                    raise ValueError(
                        "A train RunDatasetConfig must be provided when using a "
                        "run file for training."
                    )
                self._train_dataset = RunDataset(
                    Path(self.train_dataset), self.train_run_config
                )
            else:
                self._train_dataset = TriplesDataset(self.train_dataset)
        self._inference_datasets = []
        if self.inference_datasets is not None:
            if self.inference_run_config is None:
                raise ValueError(
                    "An inference RunDatasetConfig must be provided when "
                    "providing a inference datasets."
                )
            if self.inference_run_config.sampling_strategy == "single_relevant":
                raise ValueError(
                    "Inference RunDatasetConfig cannot use the single_relevant "
                    "sampling strategy."
                )
            for dataset in self.inference_datasets:
                self._inference_datasets.append(
                    RunDataset(Path(dataset), self.inference_run_config)
                )

    def train_dataloader(self) -> DataLoader:
        if self._train_dataset is None:
            raise ValueError("No training dataset found.")
        return DataLoader(
            self._train_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            collate_fn=self.collate_fn,
        )

    def val_dataloader(self) -> List[DataLoader]:
        return self.inference_dataloader()

    def predict_dataloader(self) -> List[DataLoader]:
        return self.inference_dataloader()

    def inference_dataloader(self) -> List[DataLoader]:
        return [
            DataLoader(
                dataset,
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                collate_fn=self.collate_fn,
            )
            for dataset in self._inference_datasets
        ]

    def collate_fn(self, samples: Sequence[Sample]) -> Batch:
        query_ids = []
        queries = []
        doc_ids = []
        docs = []
        targets = []
        relevances = []
        defaultdict(list)
        for sample in samples:
            query_ids.append(sample.query_id)
            queries.append(sample.query)
            doc_ids.append(sample.doc_ids)
            docs.extend(sample.docs)
            targets.extend(sample.targets)
            if sample.relevance is not None:
                relevances.append(torch.tensor(sample.relevance))
        query_encoding = self.tokenizer.encode_queries(
            queries,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_length,
        )
        doc_encoding = self.tokenizer.encode_docs(
            docs,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_length,
        )
        targets = torch.tensor(targets)
        if relevances:
            relevance = torch.nn.utils.rnn.pad_sequence(relevances, batch_first=True)
        else:
            relevance = None
        return Batch(
            tuple(query_ids),
            query_encoding,
            tuple(doc_ids),
            doc_encoding,
            targets,
            relevance,
        )