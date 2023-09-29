import pandas as pd
import numpy as np
import sspa
import sklearn
from mbpls.mbpls import MBPLS
from pathintegrate.app import launch_network_app
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import cross_val_score, GridSearchCV

class PathIntegrate:

    def __init__(self, omics_data:dict, metadata, pathway_source, sspa_scoring=sspa.sspa_SVD, min_coverage=3):
        self.omics_data = omics_data
        self.omics_data_scaled = {k: pd.DataFrame(StandardScaler().fit_transform(v), columns=v.columns, index=v.index) for k, v in self.omics_data.items()}
        self.metadata = metadata
        self.pathway_source = pathway_source
        self.pathway_dict = sspa.utils.pathwaydf_to_dict(pathway_source)
        self.sspa_scoring = sspa_scoring
        self.min_coverage = min_coverage

        # sspa_methods = {'svd': sspa.sspa_SVD, 'ssGSEA': sspa.sspa_ssGSEA, 'kpca': sspa.sspa_KPCA, 'ssClustPA': sspa.sspa_ssClustPA, 'zscore': sspa.sspa_zscore}
        self.sspa_method = self.sspa_scoring
        self.sspa_scores_mv = None
        self.sspa_scores_sv = None
        self.coverage = self.get_multi_omics_coverage()

        self.mv = None
        self.sv = None

        self.labels = pd.factorize(self.metadata)[0]
    
    def get_multi_omics_coverage(self):
        all_molecules = sum([i.columns.tolist() for i in self.omics_data.values()], [])
        coverage = {k: len(set(all_molecules).intersection(set(v))) for k, v in self.pathway_dict.items()}
        return coverage

    def MultiView(self, ncomp=2):
        print('Generating pathway scores...')
        sspa_scores_ = [self.sspa_method(self.pathway_source, self.min_coverage) for i in self.omics_data_scaled.values()]
        sspa_scores = [sspa_scores_[n].fit_transform(i) for n, i in enumerate(self.omics_data_scaled.values())]
        # sspa_scores = [self.sspa_method(self.pathway_source, self.min_coverage).fit_transform(i) for i in self.omics_data_scaled.values()]
        # sspa_scores = [self.sspa_method(i, self.pathway_source, self.min_coverage, return_molecular_importance=True) for i in self.omics_data.values()]

        self.sspa_scores_mv = dict(zip(self.omics_data.keys(), sspa_scores))
        print('Fitting MultiView model')
        mv = MBPLS(n_components=ncomp)
        mv.fit([i.copy(deep=True) for i in self.sspa_scores_mv.values()], self.labels)

        # compute VIP and scale VIP across omics
        vip_scores = VIP_multiBlock(mv.W_, mv.Ts_, mv.P_, mv.V_)
        vip_df = pd.DataFrame(vip_scores, index=sum([i.columns.tolist() for i in self.sspa_scores_mv.values()], []))
        vip_df['Name'] = vip_df.index.map(dict(zip(self.pathway_source.index, self.pathway_source['Pathway_name'])))
        vip_df['Source'] = sum([[k] * v.shape[1] for k, v in self.sspa_scores_mv.items()], [])
        vip_df['VIP_scaled'] = vip_df.groupby('Source')[0].transform(lambda x: StandardScaler().fit_transform(x.values[:,np.newaxis]).ravel())
        vip_df['VIP'] = vip_scores
        mv.name = 'MultiView'

        # only some sspa methods can return the molecular importance
        if hasattr(sspa_scores_[0], 'molecular_importance'):
            mv.molecular_importance = dict(zip(self.omics_data.keys(), [i.molecular_importance for i in sspa_scores_]))
        mv.beta = mv.beta_.flatten()
        mv.vip = vip_df
        mv.omics_names = list(self.omics_data.keys())
        mv.sspa_scores = self.sspa_scores_mv
        mv.coverage = self.coverage
        self.mv = mv

        return self.mv

    def SingleView(self, model=sklearn.linear_model.LogisticRegression, model_params=None):
        """Fits a PathIntegrate SingleView model using an SKLearn-compatible predictive model.

        Args:
            model (object, optional): SKlearn prediction model class. Defaults to sklearn.linear_model.LogisticRegression.
            model_params (_type_, optional): Model-specific hyperparameters. Defaults to None.

        Returns:
            object: Fitted PathIntegrate SingleView model.
        """
        concat_data = pd.concat(self.omics_data_scaled.values(), axis=1)
        print('Generating pathway scores...')

        sspa_scores = self.sspa_method(self.pathway_source, self.min_coverage)
        self.sspa_scores_sv = sspa_scores.fit_transform(concat_data)
       
        if model_params:
            sv = model(**model_params)
        else:
            sv = model()
        print('Fitting SingleView model')

        sv.fit(X=self.sspa_scores_sv, y=self.labels)
        sv.sspa_scores = self.sspa_scores_sv
        sv.name = 'SingleView'
        sv.coverage = self.coverage

        # only some sspa methods can return the molecular importance
        if hasattr(sspa_scores, 'molecular_importance'):
            sv.molecular_importance = sspa_scores.molecular_importance
        self.sv = sv

        return self.sv
    
    # cross-validation approaches
    def SingleViewCV(self, model=sklearn.linear_model.LogisticRegression, model_params=None, cv_params=None):

        # concatenate omics - unscaled to avoid data leakage
        concat_data = pd.concat(self.omics_data.values(), axis=1)

        # Set up sklearn pipeline
        pipe_sv = sklearn.pipeline.Pipeline([
            ('Scaler', StandardScaler().set_output(transform="pandas")),
            ('sspa', self.sspa_method(self.pathway_source, self.min_coverage)),
            ('sv', model(**model_params))
        ])

        cv_res = cross_val_score(pipe_sv, X=concat_data, y=self.labels, **cv_params)
        return cv_res
    
    def SingleViewGridSearchCV(self, param_grid, model=sklearn.linear_model.LogisticRegression, grid_search_params=None):
        # concatenate omics - unscaled to avoid data leakage
        concat_data = pd.concat(self.omics_data.values(), axis=1)

        # Set up sklearn pipeline
        pipe_sv = sklearn.pipeline.Pipeline([
            ('Scaler', StandardScaler().set_output(transform="pandas")),
            ('sspa', self.sspa_method(self.pathway_source, self.min_coverage)),
            ('model', model())
        ])

        # Set up cross-validation
        grid_search = GridSearchCV(pipe_sv, param_grid=param_grid, **grid_search_params)
        grid_search.fit(X=concat_data, y=self.labels)
        return grid_search

    def MultiViewCV(self):
        # Set up sklearn pipeline
        pipe_mv = sklearn.pipeline.Pipeline([
            ('sspa', self.sspa_method(self.pathway_source, self.min_coverage)),
            ('mbpls', MBPLS(n_components=2))
        ])

        # Set up cross-validation
        cv_res = cross_val_score(pipe_mv, X=[i.copy(deep=True) for i in self.omics_data.values()], y=self.labels)
        return cv_res

    def MultiViewGridSearchCV(self):
        pass



def VIP_multiBlock(x_weights, x_superscores, x_loadings, y_loadings):
    # stack the weights from all blocks 
    weights = np.vstack(x_weights)
    # calculate product of sum of squares of superscores and y loadings
    sumsquares = np.sum(x_superscores**2, axis=0) * np.sum(y_loadings**2, axis=0)
    # p = number of variables - stack the loadings from all blocks
    p = np.vstack(x_loadings).shape[0]
    
    # VIP is a weighted sum of squares of PLS weights 
    vip_scores = np.sqrt(p * np.sum(sumsquares*(weights**2), axis=1) / np.sum(sumsquares))
    return vip_scores