import re
import warnings
import pandas as pd
import numpy as np

from formulaic import model_matrix
from typing import Optional, Tuple, List, Union
from pyfixest.exceptions import InvalidReferenceLevelError


def model_matrix_fixest(
    fml: str,
    data: pd.DataFrame,
    weights: Optional[str] = None,
    drop_intercept=False,
    i_ref1: Optional[Union[List, str, int]] = None,
    i_ref2: Optional[Union[List, str, int]] = None,
) -> Tuple[
    pd.DataFrame,  # Y
    pd.DataFrame,  # X
    Optional[pd.DataFrame],  # I
    Optional[pd.DataFrame],  # fe
    np.ndarray,  # na_index
    np.ndarray,  # fe_na
    str,  # na_index_str
    Optional[List[str]],  # z_names
    Optional[str],  # weights
    bool,  # has_weights,
    Optional[List[str]],  # icovars (list of variables interacted with i() syntax)
]:
    """
    Create model matrices for fixed effects estimation.

    This function preprocesses the data and then calls `formulaic.model_matrix()`
    to create the model matrices.

    Args:
        fml (str): A two-sided formula string using fixest formula syntax.
        weights (str or None): Weights as a string if provided, or None if no weights, e.g., "weights".
        data (pd.DataFrame): The input DataFrame containing the data.
        drop_intercept (bool): Whether to drop the intercept from the model matrix. Default is False. If True, the intercept is dropped ex post from the model matrix
                               created by formulaic.
        i_ref1 (str or list): The reference level for the first variable in the i() syntax.
        i_ref2 (str or list): The reference level for the second variable in the i() syntax.

    Returns:
        Tuple[
            pd.DataFrame,  # Y
            pd.DataFrame,  # X
            Optional[pd.DataFrame],  # I
            Optional[pd.DataFrame],  # fe
            np.array,  # na_index
            np.array,  # fe_na
            str,  # na_index_str
            Optional[List[str]],  # z_names
            Optional[str],  # weights
            bool  # has_weights
            Optional[List[str]]

        ]: A tuple of the following elements:
            - Y: A DataFrame of the dependent variable.
            - X: A DataFrame of the covariates. If `combine = True`, contains covariates and fixed effects as dummies.
            - I: A DataFrame of the Instruments, None if no IV.
            - fe: A DataFrame of the fixed effects, None if no fixed effects specified. Only applicable if `combine = False`.
            - na_index: An array with indices of dropped columns.
            - fe_na: An array with indices of dropped columns due to fixed effect singletons or NaNs in the fixed effects.
            - na_index_str: na_index, but as a comma-separated string. Used for caching of demeaned variables.
            - z_names: Names of all covariates, minus the endogenous variables, plus the instruments. None if no IV.
            - weights: Weights as a string if provided, or None if no weights, e.g., "weights".
            - has_weights: A boolean indicating whether weights are used.
            - icovars: A list of interaction variables provided via `i()`. None if no interaction variables via `i()` provided.

    Attributes:
        list or None: icovars - A list of interaction variables. None if no interaction variables via `i()` provided.
    """

    fml = fml.replace(" ", "")
    _is_iv = _check_is_iv(fml)

    _ivars = _find_ivars(fml)[0]

    if _ivars:
        if len(_ivars) == 2:
            # check if _ivars[1] is continuous, i.e. a float or int, if not, throw an error
            if not _is_numeric(data[_ivars[1]]):
                raise ValueError(
                    f"The second variable in the i() syntax must be numeric, but it is of type {data[_ivars[1]].dtype}."
                )

    # step 1: deparse formula
    fml_parts = fml.split("|")
    depvar, covar = fml_parts[0].split("~")

    # covar to : interaction (as formulaic does not know about i() syntax
    for x in covar.split("+"):
        # id there an i() interaction?
        is_ivar = _find_ivars(x)
        # if yes:
        if is_ivar[1]:
            # if a reference level i_ref1 is set: code contrast as C(var, contr.treatment(base=i_ref1[0]))
            # if no reference level is set: code contrast as C(var)
            if i_ref1:
                inner_C = f"C({_ivars[0]},contr.treatment(base={i_ref1[0]}))"
            else:
                inner_C = f"C({_ivars[0]})"

            # if there is a second variable interacted via i() syntax, i.e. i(var1, var2),
            # then code contrast as C(var1, contr.treatment(base=i_ref1[0])):var2, where var2 = i_ref2[1]
            if len(_ivars) == 2:
                interact_vars = f"{inner_C}:{_ivars[1]}"
            elif len(_ivars) == 1:
                interact_vars = f"{inner_C}"
            else:
                raise ValueError(
                    "Something went wrong with the i() syntax. Please report this issue to the package author via github."
                )
            covar = covar.replace(x, interact_vars)
            break

    # should any variables be dropped from the model matrix
    # (e.g., reference level dummies, if specified)
    # _check_i_refs(_ivars, i_ref1, i_ref2, data)
    _check_i_refs2(_ivars, i_ref1, i_ref2, data)

    if len(fml_parts) == 3:
        fval, fml_iv = fml_parts[1], fml_parts[2]
    elif len(fml_parts) == 2:
        if _is_iv:
            fval, fml_iv = "0", fml_parts[1]
        else:
            fval, fml_iv = fml_parts[1], None
    else:
        fval = "0"
        fml_iv = None

    if _is_iv:
        endogvar, instruments = fml_iv.split("~")
    else:
        endogvar, instruments = None, None

    # step 2: create formulas
    fml_exog = f"{depvar}~{covar}"
    if _is_iv:
        fml_iv_full = f"{fml_iv}+{covar}-{endogvar}"
    # clean fixed effects
    if fval != "0":
        fe, fe_na = _clean_fe(data, fval)
        # fml_exog += " | " + fval
    else:
        fe = None
        fe_na = None
    # fml_iv already created

    Y, X = model_matrix(fml_exog, data)

    X_is_empty = False  # special case: sometimes it is useful to run models "Y ~ 0 | f1" to demean Y + to use the predict method
    if X.shape[1] == 0:
        X_is_empty = True

    # if int, turn, Y into int64, else float64
    # if Y is int
    if Y.dtypes[0] == "int64":
        pass
    else:
        Y = Y.astype("float64")
    for x in X.columns:
        if X[x].dtype == "int64":
            pass
        else:
            X[x] = X[x].astype("float64")

    if _is_iv:
        endogvar, Z = model_matrix(fml_iv_full, data)
    else:
        endogvar, Z = None, None

    Y, X, endogvar, Z = [
        pd.DataFrame(x) if x is not None else x for x in [Y, X, endogvar, Z]
    ]

    # check if Y, endogvar have dimension (N, 1) - else they are non-numeric
    if Y.shape[1] > 1:
        raise TypeError(
            f"The dependent variable must be numeric, but it is of type {data[depvar].dtype}."
        )
    if endogvar is not None:
        if endogvar.shape[1] > 1:
            raise TypeError(
                f"The endogenous variable must be numeric, but it is of type {data[endogvar].dtype}."
            )

    # step 3: catch NaNs (before converting to numpy arrays)
    na_index_stage2 = data.index.difference(Y.index).tolist()

    if _is_iv:
        na_index_stage1 = data.index.difference(Z.index).tolist()
        diff1 = list(set(na_index_stage1) - set(na_index_stage2))
        diff2 = list(set(na_index_stage2) - set(na_index_stage1))
        if diff1:
            Y.drop(diff1, axis=0, inplace=True)
            X.drop(diff1, axis=0, inplace=True)
        if diff2:
            Z.drop(diff2, axis=0, inplace=True)
            endogvar.drop(diff2, axis=0, inplace=True)
        na_index = list(set(na_index_stage1 + na_index_stage2))
    else:
        na_index = na_index_stage2

    columns_to_drop = _get_i_refs_to_drop(_ivars, i_ref1, i_ref2, X)

    if columns_to_drop:
        if not X_is_empty:
            X.drop(columns_to_drop, axis=1, inplace=True)
            if _is_iv:
                Z.drop(columns_to_drop, axis=1, inplace=True)

    # drop reference level, if specified
    # ivars are needed for plotting of all interacted variables via iplot()

    _icovars = _get_icovars(_ivars, X)

    if fe is not None:
        fe.drop(na_index, axis=0, inplace=True)
        # drop intercept
        if not X_is_empty:
            # drop intercept. intercept is present unless there is i() interaction AND a reference level is set, in which case a "0" was added to the fml above
            if "Intercept" in X.columns:
                X.drop("Intercept", axis=1, inplace=True)
        if _is_iv:
            if "Intercept" in Z.columns:
                Z.drop("Intercept", axis=1, inplace=True)

        # drop NaNs in fixed effects (not yet dropped via na_index)
        fe_na_remaining = list(set(fe_na) - set(na_index))
        if fe_na_remaining:
            Y.drop(fe_na_remaining, axis=0, inplace=True)
            if not X_is_empty:
                X.drop(fe_na_remaining, axis=0, inplace=True)
            fe.drop(fe_na_remaining, axis=0, inplace=True)
            if _is_iv:
                Z.drop(fe_na_remaining, axis=0, inplace=True)
                endogvar.drop(fe_na_remaining, axis=0, inplace=True)
            na_index += fe_na_remaining
            na_index = list(set(na_index))

    # drop intercept if specified in feols() call - mostly handy for did2s()
    if drop_intercept:
        if "Intercept" in X.columns:
            X.drop("Intercept", axis=1, inplace=True)
        if _is_iv:
            if "Intercept" in Z.columns:
                Z.drop("Intercept", axis=1, inplace=True)

    na_index_str = ",".join(str(x) for x in na_index)

    return Y, X, fe, endogvar, Z, na_index, na_index_str, _icovars, X_is_empty


def _find_ivars(x):
    """
    Find interaction variables in i() syntax.
    Args:
        x (str): A string containing the interaction variables in i() syntax.
    Returns:
        list: A list of interaction variables or None
    """

    i_match = re.findall(r"i\((.*?)\)", x)

    if i_match:
        return i_match[0].split(","), "i"
    else:
        return None, None


def _check_is_iv(fml):
    """
    Check if the formula contains an IV.
    Args:
        fml (str): The formula string.
    Returns:
        bool: True if the formula contains an IV, False otherwise.

    """
    # check if ~ contained twice in fml
    if fml.count("~") == 1:
        _is_iv = False
    elif fml.count("~") == 2:
        _is_iv = True
    else:
        raise ValueError("The formula must contain at most two '~'.")

    return _is_iv


def _clean_fe(data: pd.DataFrame, fval: str) -> Tuple[pd.DataFrame, List[int]]:
    """
    Clean and transform fixed effects in a DataFrame.

    This is a helper function used in `_model_matrix_fixest()`. The function converts
    the fixed effects to integers and marks fixed effects with NaNs. It's important
    to note that NaNs are not removed at this stage; this is done in `_model_matrix_fixest()`.

    Args:
        data (pd.DataFrame): The input DataFrame containing the data.
        fval (str): A string describing the fixed effects, e.g., "fe1 + fe2".

    Returns:
        Tuple[pd.DataFrame, List[int]]: A tuple containing two items:
            - fe (pd.DataFrame): The DataFrame with cleaned fixed effects. NaNs are
            present in this DataFrame.
            - fe_na (List[int]): A list of columns in 'fe' that contain NaN values.
    """

    fval_list = fval.split("+")

    # find interacted fixed effects via "^"
    interacted_fes = [x for x in fval_list if len(x.split("^")) > 1]

    for x in interacted_fes:
        vars = x.split("^")
        data[x] = data[vars].apply(
            lambda x: "^".join(x.dropna().astype(str)) if x.notna().all() else np.nan,
            axis=1,
        )

    fe = data[fval_list]

    for x in fe.columns:
        if fe[x].dtype != "category":
            if len(fe[x].unique()) == fe.shape[0]:
                raise ValueError(
                    f"Fixed effect {x} has only unique values. " "This is not allowed."
                )

    fe_na = fe.isna().any(axis=1)
    fe = fe.apply(lambda x: pd.factorize(x)[0])
    fe_na = fe_na[fe_na].index.tolist()

    return fe, fe_na


def _get_icovars(_ivars: List[str], X: pd.DataFrame) -> Optional[List[str]]:
    """
    Get all interacted variables via i() syntax. Required for plotting of all interacted variables via iplot().
    Args:
        _ivars (list): A list of interaction variables.
        X (pd.DataFrame): The DataFrame containing the covariates.
    Returns:
        list: A list of interacted variables or None.
    """
    if _ivars is not None:
        x_names = X.columns.tolist()
        if len(_ivars) == 2:
            _icovars = [
                s
                for s in x_names
                if s.startswith("C(" + _ivars[0]) and s.endswith(_ivars[1])
            ]
        else:
            _icovars = [
                s for s in x_names if s.startswith("C(" + _ivars[0]) and s.endswith("]")
            ]
    else:
        _icovars = None

    return _icovars


def _check_i_refs2(ivars, i_ref1, i_ref2, data) -> None:
    """
    Check if the reference level have the same type as the variable (if not, string matching might fail).
    Args:
        ivars (list): A list of interaction variables of maximum length 2.
        i_ref1 (list): A list of reference levels for the first variable in the i() syntax.
        i_ref2 (list): A list of reference levels for the second variable in the i() syntax.
        data (pd.DataFrame): The DataFrame containing the covariates.
    Returns:
        None
    """

    if ivars:
        ivar1 = ivars[0]
        if len(ivars) == 2:
            ivar2 = ivars[1]

        if i_ref1:
            # check that the type of each value in i_ref1 is the same as the type of the variable
            ivar1_col = data[ivar1]
            for i in i_ref1:
                if pd.api.types.is_integer_dtype(ivar1_col) and not isinstance(i, int):
                    raise InvalidReferenceLevelError(
                        f"If the first interacted variable via i() syntax, '{ivar1}', is of type 'int', the reference level {i} must also be of type 'int', but it is of type {type(i)}."
                    )
                if pd.api.types.is_float_dtype(ivar1_col) and not isinstance(i, float):
                    raise InvalidReferenceLevelError(
                        f"If the first interacted variable via i() syntax, '{ivar1}', is of type 'float', the reference level {i} must also be of type 'float', but it is of type {type(i)}."
                    )
        if i_ref2:
            ivar2_col = data[ivar2]
            for i in i_ref2:
                if pd.api.types.is_integer_dtype(ivar2_col) and not isinstance(i, int):
                    raise InvalidReferenceLevelError(
                        f"If the second interacted variable via i() syntax, '{ivar2}', is of type 'int', the reference level {i} must also be of type 'int', but it is of type {type(i)}."
                    )
                if pd.api.types.is_float_dtype(ivar2_col) and not isinstance(i, float):
                    raise InvalidReferenceLevelError(
                        f"If the second interacted variable via i() syntax, '{ivar2}', is of type 'float', the reference level {i} must also be of type 'float', but it is of type {type(i)}."
                    )


def _get_i_refs_to_drop(_ivars, i_ref1, i_ref2, X):
    """
    Collect all variables that (still) need to be dropped as reference levels from the model matrix.
    Args:
        _ivars (list): A list of interaction variables of maximum length 2.
        i_ref1 (list): A list of reference levels for the first variable in the i() syntax.
        i_ref2 (list): A list of reference levels for the second variable in the i() syntax.
        X (pd.DataFrame): The DataFrame containing the covariates.
    """

    columns_to_drop = []

    # now drop reference levels / variables before collecting variable names
    if _ivars:
        if i_ref1:
            # different logic for one vs two interaction variables in i() due to how contr.treatment() works
            if len(_ivars) == 1:
                if len(i_ref1) == 1:
                    pass  # do nothing, reference level already dropped via contr.treatment()
                else:
                    i_ref_to_drop = i_ref1[1:]
                    # collect all column names that contain the reference level
                    for x in i_ref_to_drop:
                        # decode formulaic naming conventions
                        columns_to_drop += [
                            s
                            for s in X.columns
                            if f"C({_ivars[0]},contr.treatment(base={i_ref1[0]}))[T.{x}]"
                            in s
                        ]
            elif len(_ivars) == 2:
                i_ref_to_drop = i_ref1
                for x in i_ref_to_drop:
                    # decode formulaic naming conventions
                    columns_to_drop += [
                        s
                        for s in X.columns
                        if f"C({_ivars[0]},contr.treatment(base={i_ref1[0]}))[T.{x}]:{_ivars[1]}"
                        in s
                    ]
            else:
                raise ValueError(
                    "Something went wrong with the i() syntax. Please report this issue to the package author via github."
                )
        if i_ref2:
            raise ValueError("The function argument `i_ref2` is not yet supported.")

    return columns_to_drop


def _is_numeric(column):
    try:
        pd.to_numeric(column)
        return True
    except ValueError:
        return False
