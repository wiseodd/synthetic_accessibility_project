"""
Materials Precursor Score
=========================

Contains functions for calculating the Materials Precursor Score (MPScore).

Usage
=====

Firstly, initialise the MPScore score. Optimal parameters are provided
by default to initalise the correct fingerprint respentation.
Then call the restore function to load the trained model.
The calibrated model is available in `models/mpscore_calibrated.joblib`,
which is provided to the restore function by default.
The `get_score_from_smiles` function can then be used to return a synthetic accessibility score
(lower is more accessible)
This returns the probability that a molecules belongs to the difficult-to-synthesise class.
"""

from collections import defaultdict
from functools import partial
from operator import sub
from pathlib import Path
from ast import literal_eval
from random import random
from unittest import result
import joblib
import numpy as np
from rdkit.Chem import AllChem
from scipy.sparse import data
from scipy.special import logit
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    fbeta_score,
)
from sklearn.model_selection import KFold, train_test_split
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib import cm
from matplotlib.collections import LineCollection
import seaborn as sns
import json
from rdkit import Chem
from tqdm import tqdm
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from copy import copy
from math import log
from sklearn.dummy import DummyClassifier
import random
from sklearn.linear_model import LogisticRegression


def get_fingerprint_as_bit_counts(
    mol: AllChem.Mol, return_info=False, nbits=1024, radius=2
):
    """
    Gets Morgan fingerprint bit counts.

    Args:
        mol: The RDKit molecule
        to have its fingerprints calculated.
        return_info: Returns the fingerprint mapping from fragments to bits.
    """
    info = dict()
    # Ensure molecules has hydrogens added for consistency.
    mol = AllChem.AddHs(mol)
    fp = AllChem.GetMorganFingerprintAsBitVect(
        mol=mol, radius=radius, nBits=nbits, bitInfo=info,
    )
    fp = list(fp)
    for bit, activators in info.items():
        fp[bit] = len(activators)
    if return_info:
        # Return fingerprint mapping, if requested.
        return fp, info
    return fp


class MPScore:
    """Represents the Materials Precursor Score.

    Attributes:
        model: The sklearn classification model.
    """

    def __init__(
        self,
        random_state=32,
        processes=-1,
        param_path="hyperparameters/optimal_params.json",
    ):
        """Initialise the MPScore.

        Args:
            random_state: Seed for random number generator.
            Used during the training procedure and cross-validation process.
        """
        if not param_path:
            # Use default parameters
            self._fp_radius = 2
            self._fp_bit_length = 1024
            self.model = RandomForestClassifier(
                n_jobs=processes,
                random_state=random_state,
                class_weight="balanced",
                criterion="gini",
            )
        if param_path:
            with open(str(param_path)) as f:
                params = dict(json.load(f))
            print(f"Intialising model using params from {param_path}")
            self._fp_radius = params.pop("fp_radius")
            self._fp_bit_length = params.pop("fp_bit_length")
            self.model = RandomForestClassifier(
                n_jobs=processes,
                random_state=random_state,
                **params,
                class_weight="balanced",
                criterion="gini",
            )

    def restore(
        self,
        model_path=str(
            Path("..").resolve().joinpath("models/mpscore_calibrated.joblib")
        ),
    ):
        print(f"Restoring parameters from {model_path}")
        self.model = joblib.load(model_path)

    def cross_validate(self, data):
        """
        Args:
            data: SA classification data.
        """
        x = np.array([np.array(fp) for fp in data["fingerprint"]])
        y = data["synthesisable"].to_numpy()
        # Cross-validation is used to approximate the final score of the MPScore.
        cv = KFold(n_splits=5, shuffle=True, random_state=32)
        # Train 5 models using cross-validation.
        predictions_combined = []
        y_test_combined = []
        metrics = {
            "Accuracy": accuracy_score,
            "Precision (Difficult-to-synthesise)": partial(
                precision_score, pos_label=0, zero_division=0
            ),
            "Recall (Difficult-to-synthesise)": partial(
                recall_score, pos_label=0, zero_division=0
            ),
            "Recall (Easy-to-synthesise)": partial(
                recall_score, pos_label=1, zero_division=0
            ),
            "Precision (Easy-to-synthesise)": partial(
                precision_score, pos_label=1, zero_division=0
            ),
            "F1": partial(f1_score, pos_label=1),
            "FBeta (Beta = 4/10)": partial(
                fbeta_score, beta=0.4, pos_label=1, zero_division=0
            ),
            "FBeta (Beta = 3/10)": partial(
                fbeta_score, beta=0.3, pos_label=1, zero_division=0
            ),
            "FBeta (Beta = 2/10)": partial(
                fbeta_score, beta=0.2, pos_label=1, zero_division=0
            ),
            "FBeta (Beta = 1/10)": partial(
                fbeta_score, beta=0.1, pos_label=1, zero_division=0
            ),
        }
        results = defaultdict(list)
        for i, (train_ind, test_ind) in enumerate(cv.split(x, y)):
            x_train, x_test = x[train_ind], x[test_ind]
            y_train, y_test = y[train_ind], y[test_ind]
            # Make a copy of the model to avoid re-training after calibration
            model = copy(self.model)
            model.fit(X=x_train, y=y_train)
            # Keep probabilities for the positive outcomes.
            test_predictions = [i for i in model.predict(x_test)]
            for metric in metrics:
                score = metrics[metric](y_test, test_predictions)
                results[metric].append(score)
            predictions_combined.extend(test_predictions)
            y_test_combined.extend(y_test)
        fps = 0
        tps = 0
        tns = 0
        fns = 0
        for i, predicted_y in enumerate(predictions_combined):
            actual_y = y_test_combined[i]
            # Positive sample
            if actual_y == 1:
                if actual_y == predicted_y:
                    tps += 1
                elif actual_y != predicted_y:
                    fns += 1
            elif actual_y == 0:
                if actual_y == predicted_y:
                    tns += 1
                elif actual_y != predicted_y:
                    fps += 1
        for metric in results:
            av = np.mean(results[metric])
            std = np.std(results[metric])
            print(f"Score: {metric}     Average: {av}      Std: {std}")
        print(
            f"Confusion: {confusion_matrix(y_test_combined, predictions_combined)}"
        )
        print(f"Total Predictions: {len(y_test_combined)}")
        print(f"Total False Positives: {fps}")
        print(f"Total False Negatives: {fns}")
        print(f"Total True Positives: {tps}")
        print(f"Total True Negatives: {tns}")
        results["TNs"] = tns
        results["FPs"] = fps
        results["FNs"] = fns
        results["TPs"] = tps
        return results

    def train_using_entire_dataset(
        self, data: pd.DataFrame, calibrate=True
    ) -> None:
        """Trains the model on the entire dataset.

        Args:
            data: Molecules labelled as synthesisable/unsynthesisable.
        """
        X = np.array([np.array(fp) for fp in data["fingerprint"]])
        y = data["synthesisable"].to_numpy()
        print(
            f"There are {sum(y)} molecules labelled indeas synthesisable (which has a value of 1)"
        )
        print(
            f"There are {len(y)-sum(y)} labelled as unsynthesisable (which has a value of 0)"
        )
        if calibrate:
            X_train, X_calib, y_train, y_calib = train_test_split(
                X, y, random_state=32
            )
            self.model.fit(X_train, y_train)
            clf = CalibratedClassifierCV(
                self.model, cv="prefit", method="sigmoid"
            )
            clf.fit(X_calib, y_calib)
            print("Finished training calibrated model on entire dataset")
            self.calibrated_model = clf
        else:
            self.model.fit(X, y)
            print("Finished training model on entire dataset")

    def load_data(self, data_path):
        """Loads the SA classification dataset.

        Args:
            data_path: Path to the JSON file containing data.
        """
        if data_path.endswith(".json"):
            return pd.read_json(data_path)

    def dump(self, dump_path):
        """Dumps the model to a file.

        Args:
            dump_path: Path to dump the model to.
        """
        if self.calibrated_model:
            calibrated_path = dump_path.split(".")
            calibrated_path += "_calibrated.json"
            joblib.dump(self.calibrated_model, dump_path)
        else:
            joblib.dump(self.model, dump_path)

    def predict(self, mol):
        """Predict SA of molecule using RF model.

        Args:
            mol: Molecule to have SA calculated.
        Returns:
            int: Prediction from model.
        """
        fp = np.array(get_fingerprint_as_bit_counts(mol)).reshape(1, -1)
        return int(self.model.predict(fp))

    def get_score_from_smiles(self, smiles, return_probability=True):
        """Gets MPScore from SMILES string of molecule.

        Args:
            smiles: SMILES string of molecule
            return_probability: Probability the molecule belongs to the difficult-to-synthesise class.
        Returns:
            int or float: Prediction from model - 1 if easy-to-synthesise, 0 if not.
            If return_probability, returns probability molecule belongs to the difficult-to-synthesise class. This can then be interpreted as a synthetic difficulty score.
        """
        mol = AllChem.MolFromSmiles(smiles)
        if return_probability:
            return self.predict_proba(mol)
        return self.predict(mol)

    def predict_proba(self, mol):
        """Predict SA of molecule as a probability.
        Args:
            mol: Molecule to have SA calculated.
        Returns:
            float: Probability that molecule belongs to the difficult-to-synthesise class. Interpretted as a measure of synthetic accessibility.
        """
        fp = np.array(
            get_fingerprint_as_bit_counts(
                mol, nbits=self._fp_bit_length, radius=self._fp_bit_length
            )
        ).reshape(1, -1)
        return self.model.predict_proba(fp)[0][0]

    def plot_calibration_curve(self, data):
        fig, ax = plt.subplots()

        X_train, X_test, y_train, y_test = train_test_split(
            [np.array(i) for i in data["fingerprint"].to_numpy()],
            [np.array(i) for i in data["synthesisable"].to_numpy()],
            random_state=32,
        )
        X_model_train, X_valid, y_model_train, y_valid = train_test_split(
            X_train, y_train, random_state=32
        )
        # Fit the uncalibrated random forest model
        self.model.fit(X_model_train, y_model_train)
        predicted_probs = [
            self.model.predict_proba(np.array(fp).reshape(1, -1))[0][1]
            for fp in tqdm(
                X_test, desc="Uncalibrated random forest predictions"
            )
        ]
        prob_true, prob_pred = calibration_curve(
            y_prob=predicted_probs, y_true=y_test, n_bins=10, normalize=False,
        )
        sns.lineplot(
            y=prob_pred, x=prob_true, ci=None, ax=ax, label="Random Forest"
        )

        # Sigmoid calibration
        sigmoid_clf = CalibratedClassifierCV(
            self.model, cv="prefit", method="sigmoid",
        )
        # Fit calibrated model on validation set
        sigmoid_clf.fit(X_valid, y_valid)
        sigmoid_pred = [
            sigmoid_clf.predict_proba(i.reshape(1, -1))[0][1]
            for i in tqdm(X_test, desc="Sigmoid random forest predictions")
        ]
        prob_true, prob_pred = calibration_curve(
            y_prob=sigmoid_pred, y_true=y_test, n_bins=10, normalize=False,
        )
        sns.lineplot(
            y=prob_pred,
            x=prob_true,
            ci=None,
            ax=ax,
            label="Random Forest + Sigmoid",
        )

        # Isotonic calibration
        isotonic_clf = CalibratedClassifierCV(
            self.model, cv="prefit", method="isotonic"
        )
        # Fit calibrated model on validation set
        isotonic_clf.fit(X_valid, y_valid)
        isotonic_pred = [
            isotonic_clf.predict_proba(np.array(fp).reshape(1, -1))[0][1]
            for fp in tqdm(X_test, desc="Isotonic random forest predictions")
        ]
        prob_true, prob_pred = calibration_curve(
            y_prob=isotonic_pred, y_true=y_test, n_bins=10, normalize=False,
        )
        sns.lineplot(
            y=prob_pred,
            x=prob_true,
            ci=None,
            ax=ax,
            label="Random Forest + Isotonic",
        )

        sns.lineplot(
            y=[0, 1], x=[0, 1], label="Perfect Classifier", color="black"
        )
        ax.lines[3].set_linestyle("--")
        sns.despine()
        ax.set_xlabel("Mean Predicted Value")
        ax.set_ylabel("Fraction of Positives")
        fig.savefig(str(Path("../images/Calibration_Curve.pdf")))

    def get_precision_recall_curve_data(self, data, model):
        results = defaultdict(lambda: [])
        thresholds = np.linspace(0, 0.99, 100)
        X = np.array([np.array(i) for i in data["fingerprint"].to_list()])
        y = data["synthesisable"].to_numpy()
        splits = KFold(n_splits=5, shuffle=True, random_state=32)
        for train_idx, test_idx in splits.split(X=X, y=y):
            X_train, X_test = X[train_idx], X[test_idx]
            y_train, y_test = y[train_idx], y[test_idx]
            model.fit(X_train, y_train)
            y_probs_test = model.predict_proba(X_test)
            for threshold in thresholds:
                # If probability of y greater than or equal to threshold, score a molecule as synthesisablex
                y_pred_test = [
                    1 if prob[1] > threshold else 0 for prob in y_probs_test
                ]
                metrics = [
                    "tp_test",
                    "fp_test",
                    "tn_test",
                    "fn_test",
                ]
                cm = confusion_matrix(y_test, y_pred_test,)
                tp_test = cm[1, 1]
                fp_test = cm[0, 1]
                tn_test = cm[0, 0]
                fn_test = cm[1, 0]
                if threshold not in results["thresholds"]:
                    results["thresholds"].append(threshold)
                for j, val in enumerate([tp_test, fp_test, tn_test, fn_test]):
                    pos = results["thresholds"].index(threshold)
                    if results[metrics[j]] == []:
                        results[metrics[j]] = [
                            [] for _ in range(len(thresholds))
                        ]
                    results[metrics[j]][pos].append(val)
        df = pd.DataFrame(results)
        for i in range(len(thresholds)):
            row = df.iloc[i]
            np.seterr(divide="warn")
            tp_test = sum(row["tp_test"])
            fp_test = sum(row["fp_test"])
            fn_test = sum(row["fn_test"])
            tp_rel_error = np.std(row["tp_test"]) / np.mean(row["tp_test"])
            fp_rel_error = np.std(row["fp_test"]) / np.mean(row["fp_test"])
            fn_rel_error = np.std(row["fn_test"]) / np.mean(row["fn_test"])
            p_test = round(tp_test / (tp_test + fp_test), 3)
            r_test = round(tp_test / (tp_test + fn_test), 3)
            prec_error = np.nan_to_num(
                (2 * tp_rel_error + fp_rel_error) * p_test
            )
            rec_error = np.nan_to_num(
                (2 * tp_rel_error + fn_rel_error) * r_test
            )
            results["p_error"].append(prec_error)
            results["r_error"].append(rec_error)
            results["p_test"].append(p_test)
            results["r_test"].append(r_test)
        return pd.DataFrame(results)

    def plot_precision_recall_curve(self, fig, ax, data):
        pr_data = self.get_precision_recall_curve_data(
            data, model=copy(self.model)
        )
        dummy = ProbabilityDummyClassifier(strategy="random_prob")
        log_model = LogisticRegression(random_state=32)
        dummy_pr_data = self.get_precision_recall_curve_data(data, model=dummy)
        log_pr_data = self.get_precision_recall_curve_data(
            data, model=log_model
        )
        X_log = np.nan_to_num(log_pr_data["r_test"].to_numpy())
        y_log = np.nan_to_num(log_pr_data["p_test"].to_numpy())
        X_dummy = np.nan_to_num(dummy_pr_data["r_test"].to_numpy())
        y_dummy = np.nan_to_num(dummy_pr_data["p_test"].to_numpy())
        X = pr_data["r_test"].to_numpy()
        y = pr_data["p_test"].to_numpy()
        Xy = np.array(
            [[(X[i], y[i]), (X[i + 1], y[i + 1])] for i in range(len(X) - 1)]
        )
        Xy_dummy = np.array(
            [
                [(X_dummy[i], y_dummy[i]), (X_dummy[i + 1], y_dummy[i + 1])]
                for i in range(len(X_dummy) - 1)
            ]
        )
        Xy_log = np.array(
            [
                [(X_log[i], y_log[i]), (X_log[i + 1], y_log[i + 1])]
                for i in range(len(X_log) - 1)
            ]
        )
        # Plot the error bars for each
        ax.errorbar(
            x=X,
            y=y,
            yerr=pr_data["p_error"],
            xerr=pr_data["r_error"],
            fmt="None",
            ecolor="black",
            elinewidth=0.01,
            errorevery=10,
            zorder=10,
        )
        ax.errorbar(
            x=X_dummy,
            y=y_dummy,
            yerr=dummy_pr_data["p_error"],
            xerr=dummy_pr_data["r_error"],
            fmt="None",
            ecolor="black",
            elinewidth=0.01,
            errorevery=10,
            zorder=10,
        )
        ax.errorbar(
            x=X_log,
            y=y_log,
            yerr=log_pr_data["p_error"],
            xerr=log_pr_data["r_error"],
            fmt="None",
            ecolor="black",
            elinewidth=0.01,
            errorevery=10,
            zorder=10,
        )

        viridis = cm.get_cmap("viridis", len(Xy))
        threshold_colors = [
            viridis(i) for i in pr_data["thresholds"].to_list()
        ]
        # Create a set of line segments so that we can color them individually
        # This creates the points as a N x 1 x 2 array so that we can stack points
        # together easily to get the segments. The segments array for line collection
        # needs to be (numlines) x (points per line) x 2 (for x and y)
        # from multicolored lines example
        # Converts calibrated probability from MPScore to a probability from the original random forest model
        mpscore_orig_prob = round(
            invert_calibrated_prob(
                1 - 0.21, calibrated_model=self.calibrated_model
            ),
            2,
        )
        mpscore_thresh_idx = [
            round(i, 2) for i in pr_data["thresholds"].to_list()
        ].index(mpscore_orig_prob)
        mpscore_pr = y[mpscore_thresh_idx]
        mpscore_re = X[mpscore_thresh_idx]
        print(f"MPScore precision is {mpscore_pr}")
        print(f"MPScore recall is {mpscore_re}")
        print(
            f"MPScore precision and recall calculated for probability threshold of {mpscore_orig_prob}"
        )
        ls = LineCollection(segments=Xy, linewidth=2, colors=threshold_colors)
        ls_dummy = LineCollection(
            segments=Xy_dummy, linewidth=2, colors=threshold_colors
        )
        ls_log = LineCollection(
            segments=Xy_log, linewidth=2, colors=threshold_colors
        )
        ax.add_collection(ls_dummy)
        ax.add_collection(ls)
        ax.add_collection(ls_log)
        ax.set_ylim(-0, 1.01)
        cbar = fig.colorbar(
            mappable=cm.ScalarMappable(cmap=viridis, norm=None)
        )
        cbar.minorticks_on()
        cbar.set_label("Threshold", fontsize="medium")
        cbar.ax.tick_params(labelsize="medium")
        ax.set_xlim(0, 1)
        circ = plt.Circle(
            (mpscore_re, mpscore_pr),
            0.01,
            color="black",
            fill=False,
            linewidth=1,
            zorder=10,
        )
        ax.text(
            mpscore_re + 0.085,
            mpscore_pr - 0.01,
            fontsize="medium",
            s="MPScore Threshold",
            color="black",
            alpha=0.8,
        )
        ax.text(
            mpscore_re + 0.3,
            mpscore_pr - 0.08,
            fontsize="medium",
            s=f"{mpscore_orig_prob}",
            color="black",
            alpha=0.8,
        )
        ax.text(
            0.35,
            0.3,
            fontsize="medium",
            alpha=1,
            s="Logistic",
            color="grey",
            zorder=20,
        )
        ax.text(
            0.25,
            0.16 - 0.09,
            fontsize="medium",
            alpha=1,
            s="Baseline",
            color="grey",
            zorder=20,
        )
        ax.add_artist(circ)
        ax.set_xlabel("Recall", labelpad=20, fontsize="medium")
        ax.set_ylabel("Precision", fontsize="medium")
        ax.tick_params("both", labelsize="medium")
        ax.set_title("b)", fontsize="medium")
        return fig, ax

    def plot_feature_importances(self, ax):
        importances = [[] for _ in range(1024)]
        for tree in self.model.estimators_:
            for i, importance in enumerate(tree.feature_importances_):
                importances[i].append(importance)
        importances = np.array(importances)
        importances_mean = np.mean(importances, axis=1)
        importances_stdev = np.std(importances, axis=1)
        fp_importances = {str(i): j for i, j in enumerate(importances_mean)}
        fp_stdevs = list(
            sorted(
                importances_stdev,
                key=lambda x: fp_importances[
                    str(list(importances_stdev).index(x))
                ],
                reverse=True,
            )
        )
        fp_importances = dict(
            sorted(fp_importances.items(), key=lambda x: x[1], reverse=True)
        )
        plt.tight_layout()
        palette = list(sns.color_palette("viridis", 20))[13]
        x = list(fp_importances.keys())[:20]
        height = list(fp_importances.values())[:20]
        ax.bar(x, height, width=0.75, color=palette, yerr=fp_stdevs[:20])
        ax.set_ylim([0, 0.06])
        sns.despine()
        ax.set_xlabel("Bit Number", labelpad=10, fontsize="medium")
        ax.set_ylabel("Feature Importance", fontsize="medium")
        ax.tick_params(labelsize="medium")
        ax.tick_params("y", labelsize="medium")
        ax.tick_params("x", rotation=90, labelsize=8)
        ax.set_title("a)", fontsize="large")
        return ax

    def plot_figure_5(self, data):
        fig, axes = plt.subplots(1, 2, figsize=(6.43420506434205, 3.3))
        fig, axes[1] = self.plot_precision_recall_curve(
            fig, axes[1], data=data
        )
        axes[0] = self.plot_feature_importances(axes[0])
        print("Saving figure.")
        fig.savefig(Path(__file__).parents[1].joinpath("images/Figure_5.pdf"))


def main():
    data_path = Path("../data/chemist_scores.json").resolve()
    training_data = MPScore().load_data(str(data_path))
    param_path = Path("hyperparameters/optimal_params.json")
    with open(str(param_path)) as f:
        params = dict(json.load(f))
    model = MPScore(param_path=param_path)
    training_mols = [Chem.MolFromInchi(i) for i in training_data["inchi"]]
    training_data["fingerprint"] = [
        get_fingerprint_as_bit_counts(
            mol, radius=model._fp_radius, nbits=model._fp_bit_length
        )
        for mol in training_mols
    ]
    model.train_using_entire_dataset(training_data)
    full_model_path = Path("../models/mpscore_calibrated.joblib")
    # model.dump(str(full_model_path))
    model.cross_validate(training_data)
    model.plot_figure_5(data=training_data)
    # model.plot_calibration_curve(data=training_data)


def invert_calibrated_prob(prob, calibrated_model):
    sigmoid_classifier = calibrated_model.calibrated_classifiers_[
        0
    ].calibrators[0]
    a = sigmoid_classifier.a_
    b = sigmoid_classifier.b_
    return (log((1 - prob) / prob) - b) / a


def param_type_conversion(params):
    p = []
    for param in params:
        if param.replace(".", "", 1).isdigit() or param == "None":
            p.append(literal_eval(param))
        else:
            p.append(param)
    return p


class ProbabilityDummyClassifier:
    def __init__(self, strategy):
        self.strategy = strategy

    def fit(*args, **kwargs):
        return None

    def predict_proba(self, X):
        if self.strategy == "random_prob":
            random.seed(32)
            return [[0, float(random.uniform(0, 1))] for i in range(len(X))]


if __name__ == "__main__":
    main()
