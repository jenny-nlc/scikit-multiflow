import numpy as np
import libNearestNeighbor
import logging
import copy as cp
from sklearn.cluster import KMeans
from collections import deque
from skmultiflow.core.base import StreamModel
from skmultiflow.core.utils.utils import get_dimensions


class SAMKNN(StreamModel):
    """ SAMKNN - Self Adjusting Memory (SAM) coupled with the k Nearest Neighbor classifier.

    Parameters
    ----------
    n_neighbors : int, optional (default=5)
        number of evaluated nearest neighbors.
        
    knnWeights: string, optional (default='distance')
        Type of weighting of the nearest neighbors. It must be either 'distance' 
        or 'uniform' (majority voting).
         
    maxSize : int, optional (default=5000)
         Maximum number of overall stored data points.
         
    LTMSizeProportion: float, optional (default=0.4)
        Proportion of the overall instances that may be used for the LTM. This is 
        only relevant when the maximum number(maxSize) of stored instances is reached.
        
    STMSizeAdaption : string, optional (default='maxACCApprox')
        Type of STM size adaption.
        'maxACC' calculates the Interleaved test-train error exactly for each of the 
        evaluated window sizes, which means it has often to be recalculated from the 
        scratch.
        'maxACCApprox' approximates the Interleaved test-train error and is 
        significantly faster than the exact version. If set to None, the STM is not 
        adapted at all. When additionally useLTM=false, this algorithm is simply a kNN 
        with fixed sliding window size.
        
    minSTMSize : int, optional (default=50)
        Minimum STM size which is evaluated during the STM size adaption.
        
    useLTM : boolean, optional (default=True)
        Specifies whether the LTM should be used at all.
    
    Examples
    --------
    >>> from skmultiflow.classification.lazy.sam_knn import SAMKNN
    >>> from skmultiflow.data.file_stream import FileStream
    >>> from skmultiflow.evaluation.evaluate_prequential import EvaluatePrequential
    >>> # Setup the File Stream
    >>> stream = FileStream("skmultiflow/datasets/movingSquares.csv", -1, 1)
    >>> stream.prepare_for_use()
    >>> # Setup the classifier
    >>> classifier = SAMKNN(n_neighbors=5, knnWeights='distance', maxSize=1000, STMSizeAdaption='maxACCApprox', useLTM=False)
    >>> # Setup the evaluator
    >>> evaluator = EvaluatePrequential(pretrain_size=0, max_samples=200000, batch_size=1, n_wait=100, max_time=1000,
    >>>                                 output_file=None, show_plot=True, metrics=['performance', 'kappa_t'])
    >>> # Evaluate
    >>> evaluator.evaluate(stream=stream, model=classifier)

    Notes
    -----
    The Self Adjusting Memory (SAM) [1]_ model builds an ensemble with models targeting current
    or former concepts. SAM is built using two memories: STM for the current concept, and
    the LTM to retain information about past concepts. A cleaning process is in charge of
    controlling the size of the STM while keeping the information in the LTM consistent
    with the STM.

    This modules uses the libNearestNeighbor, a C++ library used to speed up some of 
    the algorithm's computations. When invoking the library's functions it's important 
    to pass the right argument type. Although most of this framework's functionality
    will work with python standard types, the C++ library will work with 8-bit labels, 
    which is already done by the SAMKNN class, but may be absent in custom target_values that
    use SAMKNN static methods, or other custom functions that use the C++ library.

    References
    ----------
    .. [1] Losing, Viktor, Barbara Hammer, and Heiko Wersing. "Knn classifier with self adjusting memory for
       heterogeneous concept drift." In Data Mining (ICDM), 2016 IEEE 16th International Conference on,
       pp. 291-300. IEEE, 2016.

    """

    def __init__(self, n_neighbors=5, knnWeights='distance', maxSize=5000, LTMSizeProportion = 0.4, minSTMSize=50, STMSizeAdaption='maxACCApprox', useLTM=True):
        super().__init__()
        self.n_neighbors = n_neighbors
        self._STMSamples = None
        self._STMLabels = np.empty(shape=(0), dtype=np.int32)
        self._LTMSamples = None
        self._LTMLabels = np.empty(shape=(0), dtype=np.int32)
        self.maxLTMSize = LTMSizeProportion * maxSize
        self.maxSTMSize = maxSize - self.maxLTMSize
        self.minSTMSize = minSTMSize

        if STMSizeAdaption is not None:
            self.STMDistances = np.zeros(shape=(maxSize + 1, maxSize + 1))
        if knnWeights == 'distance':
            self.getLabelsFct = SAMKNN.get_distance_weighted_label
        elif knnWeights == 'uniform':
            self.getLabelsFct = SAMKNN.get_maj_label
        self.STMSizeAdaption = STMSizeAdaption
        self.useLTM = useLTM
        if useLTM:
            self.predictFct = self._predict_by_all_memories
            self.sizeCheckFct = self.size_check_STMLTM
        else:
            self.predictFct = self._predict_by_stm
            self.sizeCheckFct = self.size_check_fade_out

        self.interLeavedPredHistories = {}
        self.LTMPredHistory = deque([])
        self.STMPredHistory = deque([])
        self.CMPredHistory = deque([])

        self.trainStepCount = 0
        self.STMSizes = []
        self.LTMSizes = []
        self.numSTMCorrect = 0
        self.numLTMCorrect = 0
        self.numCMCorrect = 0
        self.numPossibleCorrectPredictions = 0
        self.numCorrectPredictions = 0
        self.classifierChoice = []
        self.predHistory = []

    @staticmethod
    def get_distances(sample, samples):
        """Calculate distances from sample to all samples."""
        return libNearestNeighbor.get1ToNDistances(sample, samples)

    def cluster_down(self, samples, labels):
        """Performs classwise kMeans++ clustering for given samples with corresponding labels. The number of samples is halved per class."""
        logging.debug('cluster Down %d' % self.trainStepCount)
        uniqueLabels = np.unique(labels)
        newSamples = np.empty(shape=(0, samples.shape[1]))
        newLabels = np.empty(shape=(0), dtype=np.int32)
        for label in uniqueLabels:
            tmpSamples = samples[labels == label]
            newLength = int(max(tmpSamples.shape[0]/2, 1))
            clustering = KMeans(n_clusters=newLength, n_init=1, random_state=0)
            clustering.fit(tmpSamples)
            newSamples = np.vstack([newSamples, clustering.cluster_centers_])
            newLabels = np.append(newLabels, label*np.ones(shape=newLength, dtype=np.int32))
        return newSamples, newLabels

    def size_check_fade_out(self):
        """Makes sure that the STM does not surpass the maximum size, only used when useLTM=False."""
        STMShortened = False
        if len(self._STMLabels) > self.maxSTMSize + self.maxLTMSize:
            STMShortened = True
            self._STMSamples = np.delete(self._STMSamples, 0, 0)
            self._STMLabels = np.delete(self._STMLabels, 0, 0)
            self.STMDistances[:len(self._STMLabels), :len(self._STMLabels)] = self.STMDistances[1:len(self._STMLabels) + 1, 1:len(self._STMLabels) + 1]

            if self.STMSizeAdaption == 'maxACCApprox':
                keyset = list(self.interLeavedPredHistories.keys())
                # if self.interLeavedPredHistories.has_key(0):
                if 0 in keyset:
                    self.interLeavedPredHistories[0].pop(0)
                for key in self.interLeavedPredHistories.keys():
                    if key > 0:
                        if key == 1:
                            self.interLeavedPredHistories.pop(0, None)
                        tmp = self.interLeavedPredHistories[key]
                        self.interLeavedPredHistories.pop(key, None)
                        self.interLeavedPredHistories[key-1] = tmp
            else:
                self.interLeavedPredHistories = {}
        return STMShortened

    def size_check_STMLTM(self):
        """Makes sure that the STM and LTM combined doe not surpass the maximum size, only used when useLTM=True."""
        STMShortened = False
        if len(self._STMLabels) + len(self._LTMLabels) > self.maxSTMSize + self.maxLTMSize:
            if len(self._LTMLabels) > self.maxLTMSize:
                self._LTMSamples, self._LTMLabels = self.cluster_down(self._LTMSamples, self._LTMLabels)
            else:
                if len(self._STMLabels) + len(self._LTMLabels) > self.maxSTMSize + self.maxLTMSize:
                    STMShortened = True
                    numShifts = int(self.maxLTMSize - len(self._LTMLabels) + 1)
                    shiftRange = range(numShifts)
                    self._LTMSamples = np.vstack([self._LTMSamples, self._STMSamples[:numShifts, :]])
                    self._LTMLabels = np.append(self._LTMLabels, self._STMLabels[:numShifts])
                    self._LTMSamples, self._LTMLabels = self.cluster_down(self._LTMSamples, self._LTMLabels)
                    self._STMSamples = np.delete(self._STMSamples, shiftRange, 0)
                    self._STMLabels = np.delete(self._STMLabels, shiftRange, 0)
                    self.STMDistances[:len(self._STMLabels),:len(self._STMLabels)] = self.STMDistances[numShifts:len(self._STMLabels)+numShifts, numShifts:len(self._STMLabels)+numShifts]
                    for i in shiftRange:
                        self.LTMPredHistory.popleft()
                        self.STMPredHistory.popleft()
                        self.CMPredHistory.popleft()
                    self.interLeavedPredHistories = {}
        return STMShortened

    def clean_samples(self, samplesCl, labelsCl, onlyLast=False):
        """Removes distance-based all instances from the input samples that contradict those in the STM."""
        if len(self._STMLabels) > self.n_neighbors and samplesCl.shape[0] > 0:
            if onlyLast:
                loopRange = [len(self._STMLabels)-1]
            else:
                loopRange = range(len(self._STMLabels))
            for i in loopRange:
                if len(labelsCl) == 0:
                    break
                samplesShortened = np.delete(self._STMSamples, i, 0)
                labelsShortened = np.delete(self._STMLabels, i, 0)
                distancesSTM = SAMKNN.get_distances(self._STMSamples[i, :], samplesShortened)
                nnIndicesSTM = libNearestNeighbor.nArgMin(self.n_neighbors, distancesSTM)[0]
                distancesLTM = SAMKNN.get_distances(self._STMSamples[i, :], samplesCl)
                nnIndicesLTM = libNearestNeighbor.nArgMin(min(len(distancesLTM), self.n_neighbors), distancesLTM)[0]
                correctIndicesSTM = nnIndicesSTM[labelsShortened[nnIndicesSTM] == self._STMLabels[i]]
                if len(correctIndicesSTM) > 0:
                    distThreshold = np.max(distancesSTM[correctIndicesSTM])
                    wrongIndicesLTM = nnIndicesLTM[labelsCl[nnIndicesLTM] != self._STMLabels[i]]
                    delIndices = np.where(distancesLTM[wrongIndicesLTM] <= distThreshold)[0]
                    samplesCl = np.delete(samplesCl, wrongIndicesLTM[delIndices], 0)
                    labelsCl = np.delete(labelsCl, wrongIndicesLTM[delIndices], 0)
        return samplesCl, labelsCl

    def _partial_fit(self, x, y):
        """Processes a new sample."""
        distancesSTM = SAMKNN.get_distances(x, self._STMSamples)
        if not self.useLTM:
            self._partial_fit_by_stm(x, y, distancesSTM)
        else:
            self._partial_fit_by_all_memories(x, y, distancesSTM)

        self.trainStepCount += 1
        self._STMSamples = np.vstack([self._STMSamples, x])
        self._STMLabels = np.append(self._STMLabels, y)
        STMShortened = self.sizeCheckFct()

        self._LTMSamples, self._LTMLabels = self.clean_samples(self._LTMSamples, self._LTMLabels, onlyLast=True)

        if self.STMSizeAdaption is not None:
            if STMShortened:
                distancesSTM = SAMKNN.get_distances(x, self._STMSamples[:-1, :])

            self.STMDistances[len(self._STMLabels)-1,:len(self._STMLabels)-1] = distancesSTM
            oldWindowSize = len(self._STMLabels)
            newWindowSize, self.interLeavedPredHistories = STMSizer.getNewSTMSize(self.STMSizeAdaption, self._STMLabels, self.n_neighbors, self.getLabelsFct, self.interLeavedPredHistories, self.STMDistances, self.minSTMSize)

            if newWindowSize < oldWindowSize:
                delrange = range(oldWindowSize-newWindowSize)
                oldSTMSamples = self._STMSamples[delrange, :]
                oldSTMLabels = self._STMLabels[delrange]
                self._STMSamples = np.delete(self._STMSamples, delrange, 0)
                self._STMLabels = np.delete(self._STMLabels, delrange, 0)
                self.STMDistances[:len(self._STMLabels),:len(self._STMLabels)] = self.STMDistances[(oldWindowSize-newWindowSize):(oldWindowSize-newWindowSize)+len(self._STMLabels),(oldWindowSize-newWindowSize):(oldWindowSize-newWindowSize)+len(self._STMLabels)]

                if self.useLTM:
                    for i in delrange:
                        self.STMPredHistory.popleft()
                        self.LTMPredHistory.popleft()
                        self.CMPredHistory.popleft()

                    oldSTMSamples, oldSTMLabels = self.clean_samples(oldSTMSamples, oldSTMLabels)
                    self._LTMSamples = np.vstack([self._LTMSamples, oldSTMSamples])
                    self._LTMLabels = np.append(self._LTMLabels, oldSTMLabels)
                    self.sizeCheckFct()
        self.STMSizes.append(len(self._STMLabels))
        self.LTMSizes.append(len(self._LTMLabels))

    def _partial_fit_by_all_memories(self, sample, label, distancesSTM):
        """Predicts the label of a given sample by using the STM, LTM and the CM, only used when useLTM=True."""
        predictedLabelLTM = 0
        predictedLabelSTM = 0
        predictedLabelBoth = 0
        classifierChoice = 0
        if len(self._STMLabels) == 0:
            predictedLabel = predictedLabelSTM
        else:
            if len(self._STMLabels) < self.n_neighbors:
                predictedLabelSTM = self.getLabelsFct(distancesSTM, self._STMLabels, len(self._STMLabels))[0]
                predictedLabel = predictedLabelSTM
            else:
                distancesLTM = SAMKNN.get_distances(sample, self._LTMSamples)
                predictedLabelSTM = self.getLabelsFct(distancesSTM, self._STMLabels, self.n_neighbors)[0]
                predictedLabelBoth = \
                self.getLabelsFct(np.append(distancesSTM, distancesLTM), np.append(self._STMLabels, self._LTMLabels),
                                  self.n_neighbors)[0]

                if len(self._LTMLabels) >= self.n_neighbors:
                    predictedLabelLTM = self.getLabelsFct(distancesLTM, self._LTMLabels, self.n_neighbors)[0]
                    correctLTM = np.sum(self.LTMPredHistory)
                    correctSTM = np.sum(self.STMPredHistory)
                    correctBoth = np.sum(self.CMPredHistory)
                    labels = [predictedLabelSTM, predictedLabelLTM, predictedLabelBoth]
                    classifierChoice = np.argmax([correctSTM, correctLTM, correctBoth])
                    predictedLabel = labels[classifierChoice]
                else:
                    predictedLabel = predictedLabelSTM

        self.classifierChoice.append(classifierChoice)
        self.CMPredHistory.append(predictedLabelBoth == label)
        self.numCMCorrect += predictedLabelBoth == label
        self.STMPredHistory.append(predictedLabelSTM == label)
        self.numSTMCorrect += predictedLabelSTM == label
        self.LTMPredHistory.append(predictedLabelLTM == label)
        self.numLTMCorrect += predictedLabelLTM == label
        self.numPossibleCorrectPredictions += label in [predictedLabelSTM, predictedLabelBoth, predictedLabelLTM]
        self.numCorrectPredictions += predictedLabel == label
        return predictedLabel

    def _predict_by_all_memories(self, sample, label, distancesSTM):
        predictedLabelLTM = 0
        predictedLabelSTM = 0
        predictedLabelBoth = 0
        classifierChoice = 0
        predictedLabel = None
        if len(self._STMLabels) == 0:
            predictedLabel = predictedLabelSTM
        else:
            if len(self._STMLabels) < self.n_neighbors:
                predictedLabelSTM = self.getLabelsFct(distancesSTM, self._STMLabels, len(self._STMLabels))[0]
                predictedLabel = predictedLabelSTM
            else:
                distancesLTM = SAMKNN.get_distances(sample, self._LTMSamples)
                predictedLabelSTM = self.getLabelsFct(distancesSTM, self._STMLabels, self.n_neighbors)[0]
                distances_new = cp.deepcopy(distancesSTM)
                stm_labels_new = cp.deepcopy(self._STMLabels)
                predictedLabelBoth = \
                    self.getLabelsFct(np.append(distances_new, distancesLTM),
                                      np.append(stm_labels_new, self._LTMLabels),
                                      self.n_neighbors)[0]
                if len(self._LTMLabels) >= self.n_neighbors:
                    predictedLabelLTM = self.getLabelsFct(distancesLTM, self._LTMLabels, self.n_neighbors)[0]
                    correctLTM = np.sum(self.LTMPredHistory)
                    correctSTM = np.sum(self.STMPredHistory)
                    correctBoth = np.sum(self.CMPredHistory)
                    labels = [predictedLabelSTM, predictedLabelLTM, predictedLabelBoth]
                    classifierChoice = np.argmax([correctSTM, correctLTM, correctBoth])
                    predictedLabel = labels[classifierChoice]
                else:
                    predictedLabel = predictedLabelSTM

        return predictedLabel

    def _partial_fit_by_stm(self, sample, label, distancesSTM):
        pass

    def _predict_by_stm(self, sample, label, distancesSTM):
        """Predicts the label of a given sample by the STM, only used when useLTM=False."""
        predictedLabel = 0
        currLen = len(self._STMLabels)
        if currLen > 0:
            predictedLabel = self.getLabelsFct(distancesSTM, self._STMLabels, min(self.n_neighbors, currLen))[0]
        return predictedLabel

    def fit(self, X, y, classes = None, weight=None):
        self.partial_fit(X, y, classes, weight)

    def partial_fit(self, X, y, classes=None, weight=None):
        """Processes a new sample."""
        r, c = get_dimensions(X)
        if self._STMSamples is None:
            self._STMSamples = np.empty(shape=(0, c))
            self._LTMSamples = np.empty(shape=(0, c))

        for i in range(r):
            self._partial_fit(X[i, :], y[i])

        return self

    def predict(self, X):
        r, c = get_dimensions(X)
        predictedLabel = []
        if self._STMSamples is None:
            self._STMSamples = np.empty(shape=(0, c))
            self._LTMSamples = np.empty(shape=(0, c))

        for i in range(r):
            distancesSTM = SAMKNN.get_distances(X[i], self._STMSamples)
            predictedLabel.append(self.predictFct(X[i], None, distancesSTM))
        return predictedLabel

    def predict_proba(self, X):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def score(self, X, y):
        raise NotImplementedError

    @staticmethod
    def get_maj_label(distances, labels, numNeighbours):
        """Returns the majority label of the k nearest neighbors."""

        nnIndices = libNearestNeighbor.nArgMin(numNeighbours, distances)

        if not isinstance(labels, type(np.array([]))):
            labels = np.asarray(labels, dtype=np.int8)
        else:
            labels = np.int8(labels)

        predLabels = libNearestNeighbor.mostCommon(labels[nnIndices])

        return predLabels

    @staticmethod
    def get_distance_weighted_label(distances, labels, numNeighbours):
        """Returns the the distance weighted label of the k nearest neighbors."""
        nnIndices = libNearestNeighbor.nArgMin(numNeighbours, distances)
        sqrtDistances = np.sqrt(distances[nnIndices])
        if not isinstance(labels, type(np.array([]))):
            labels = np.asarray(labels, dtype=np.int8)
        else:
            labels = np.int8(labels)

        predLabels = libNearestNeighbor.getLinearWeightedLabels(labels[nnIndices], sqrtDistances)
        return predLabels

    def get_complexity(self):
        return 0

    def get_complexity_num_parameter_metric(self):
        return 0

    @property
    def STMSamples(self):
        return self._STMSamples

    @property
    def STMLabels(self):
        return self._STMLabels

    @property
    def LTMSamples(self):
        return self._LTMSamples

    @property
    def LTMLabels(self):
        return self._LTMLabels

    def get_info(self):
        result = ''
        result += 'avg. STMSize %f LTMSize %f' % (np.mean(self.STMSizes), np.mean(self.LTMSizes)) + '; '
        result += 'num correct STM %d LTM %d CM %d ' % (self.numSTMCorrect, self.numLTMCorrect, self.numCMCorrect) + '; '
        result += 'num correct %d/%d' % (self.numCorrectPredictions, self.numPossibleCorrectPredictions) + '\n'
        return result


class STMSizer(object):
    """Utility class to adapt the size of the sliding window of the STM."""
    @staticmethod
    def getNewSTMSize(adaptionStrategy, labels, nNeighbours, getLabelsFct, predictionHistories, distancesSTM, minSTMSize):
        """Returns the new STM size."""
        if adaptionStrategy is None:
            return len(labels), predictionHistories
        elif adaptionStrategy == 'maxACC':
            return STMSizer.getMaxAccWindowSize(labels, nNeighbours, getLabelsFct, predictionHistories, distancesSTM, minSize=minSTMSize)
        elif adaptionStrategy == 'maxACCApprox':
            return STMSizer.getMaxAccApproxWindowSize(labels, nNeighbours, getLabelsFct, predictionHistories, distancesSTM, minSize=minSTMSize)
        else:
            raise Exception('unknown driftStrategy')

    @staticmethod
    def accScore(predLabels, labels):
        """Calculates the achieved accuracy."""
        return np.sum(predLabels == labels)/float(len(predLabels))

    @staticmethod
    def getInterleavedTestTrainAcc(labels, nNeighbours, getLabelsFct, distancesSTM):
        """Calculates the interleaved test train accuracy from the scratch."""
        predLabels = []
        for i in range(nNeighbours, len(labels)):
            distances = distancesSTM[i, :i]
            predLabels.append(getLabelsFct(distances, labels[:i], nNeighbours)[0])
        return STMSizer.accScore(predLabels[:], labels[nNeighbours:]), (predLabels == labels[nNeighbours:]).tolist()

    @staticmethod
    def getInterleavedTestTrainAccPredHistory(labels, nNeighbours, getLabelsFct, predictionHistory, distancesSTM):
        """Calculates the interleaved test train accuracy incrementally by using the previous predictions."""
        for i in range(len(predictionHistory) + nNeighbours, len(labels)):
            distances = distancesSTM[i, :i]
            label = getLabelsFct(distances, labels[:i], nNeighbours)[0]
            predictionHistory.append(label == labels[i])
        return np.sum(predictionHistory)/float(len(predictionHistory)), predictionHistory

    @staticmethod
    def adaptHistories(numberOfDeletions, predictionHistories):
        """Removes predictions of the largest window size and shifts the remaining ones accordingly."""
        for i in range(numberOfDeletions):
            sortedKeys = np.sort(list(predictionHistories.keys()))
            predictionHistories.pop(sortedKeys[0], None)
            delta = sortedKeys[1]
            for j in range(1, len(sortedKeys)):
                predictionHistories[sortedKeys[j]- delta] = predictionHistories.pop(sortedKeys[j])
        return predictionHistories

    @staticmethod
    def getMaxAccWindowSize(labels, nNeighbours, getLabelsFct, predictionHistories, distancesSTM, minSize=50):
        """Returns the window size with the minimum Interleaved test-train error(exact calculation)."""
        numSamples = len(labels)
        if numSamples < 2 * minSize:
            return numSamples, predictionHistories
        else:
            numSamplesRange = [numSamples]
            while numSamplesRange[-1]/2 >= minSize:
                numSamplesRange.append(numSamplesRange[-1]/2)

            accuracies = []
            for key in predictionHistories.keys():
                if key not in (numSamples - np.array(numSamplesRange)):
                    predictionHistories.pop(key, None)

            for numSamplesIt in numSamplesRange:
                idx = int(numSamples - numSamplesIt)
                keyset = list(predictionHistories.keys())
                # if predictionHistories.has_key(idx):
                if idx in keyset:
                    accuracy, predHistory = STMSizer.getInterleavedTestTrainAccPredHistory(labels[idx:], nNeighbours, getLabelsFct, predictionHistories[idx], distancesSTM[idx:, idx:])
                else:
                    accuracy, predHistory = STMSizer.getInterleavedTestTrainAcc(labels[idx:], nNeighbours, getLabelsFct, distancesSTM[idx:, idx:])
                predictionHistories[idx] = predHistory
                accuracies.append(accuracy)
            accuracies = np.round(accuracies, decimals=4)
            bestNumTrainIdx = np.argmax(accuracies)
            windowSize = numSamplesRange[bestNumTrainIdx]

            if windowSize < numSamples:
                predictionHistories = STMSizer.adaptHistories(bestNumTrainIdx, predictionHistories)
            return int(windowSize), predictionHistories

    @staticmethod
    def getMaxAccApproxWindowSize(labels, nNeighbours, getLabelsFct, predictionHistories, distancesSTM, minSize=50):
        """Returns the window size with the minimum Interleaved test-train error(using an approximation)."""
        numSamples = len(labels)
        if numSamples < 2 * minSize:
            return numSamples, predictionHistories
        else:
            numSamplesRange = [numSamples]
            while numSamplesRange[-1]/2 >= minSize:
                numSamplesRange.append(numSamplesRange[-1]/2)
            accuracies = []
            for numSamplesIt in numSamplesRange:
                idx = int(numSamples - numSamplesIt)
                keyset = list(predictionHistories.keys())
                # if predictionHistories.has_key(idx):
                if idx in keyset:
                    accuracy, predHistory = STMSizer.getInterleavedTestTrainAccPredHistory(labels[idx:], nNeighbours, getLabelsFct, predictionHistories[idx], distancesSTM[idx:, idx:])
                # elif predictionHistories.has_key(idx-1):
                elif idx-1 in keyset:
                    predHistory = predictionHistories[idx-1]
                    predictionHistories.pop(idx-1, None)
                    predHistory.pop(0)
                    accuracy, predHistory = STMSizer.getInterleavedTestTrainAccPredHistory(labels[idx:], nNeighbours, getLabelsFct, predHistory, distancesSTM[idx:, idx:])
                else:
                    accuracy, predHistory = STMSizer.getInterleavedTestTrainAcc(labels[idx:], nNeighbours, getLabelsFct, distancesSTM[idx:, idx:])
                predictionHistories[idx] = predHistory
                accuracies.append(accuracy)
            accuracies = np.round(accuracies, decimals=4)
            bestNumTrainIdx = np.argmax(accuracies)
            if bestNumTrainIdx > 0:
                moreAccurateIndices = np.where(accuracies > accuracies[0])[0]
                for i in moreAccurateIndices:
                    idx = int(numSamples - numSamplesRange[i])
                    accuracy, predHistory = STMSizer.getInterleavedTestTrainAcc(labels[idx:], nNeighbours, getLabelsFct, distancesSTM[idx:, idx:])
                    predictionHistories[idx] = predHistory
                    accuracies[i] = accuracy
                accuracies = np.round(accuracies, decimals=4)
                bestNumTrainIdx = np.argmax(accuracies)
            windowSize = numSamplesRange[bestNumTrainIdx]

            if windowSize < numSamples:
                predictionHistories = STMSizer.adaptHistories(bestNumTrainIdx, predictionHistories)
            return int(windowSize), predictionHistories
