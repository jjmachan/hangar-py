from contextlib import ExitStack
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Tuple, Union, Dict

import lmdb
import numpy as np

from .backends import (
    parse_user_backend_opts,
)
from .txnctx import TxnRegister
from .records.hashmachine import schema_hash_digest
from .records.parsing import (
    arrayset_record_count_range_key,
    arrayset_record_schema_db_key_from_raw_key,
    arrayset_record_schema_db_val_from_raw_val,
    arrayset_record_schema_raw_val_from_db_val,
    generate_sample_name,
    hash_schema_db_key_from_raw_key,
)
from .records.queries import RecordQuery
from .utils import cm_weakref_obj_proxy, is_suitable_user_key, is_ascii
from .columns import AsetTxn, Sample, Subsample, ModifierTypes

KeyType = Union[str, int]

"""
Constructor and Interaction Class for Arraysets
--------------------------------------------------
"""


class Arraysets(object):
    """Common access patterns and initialization/removal of arraysets in a checkout.

    This object is the entry point to all tensor data stored in their individual
    arraysets. Each arrayset contains a common schema which dictates the general
    shape, dtype, and access patters which the backends optimize access for. The
    methods contained within allow us to create, remove, query, and access these
    collections of common tensors.
    """

    def __init__(self,
                 mode: str,
                 repo_pth: Path,
                 arraysets: Dict[str, ModifierTypes],
                 hashenv: Optional[lmdb.Environment] = None,
                 dataenv: Optional[lmdb.Environment] = None,
                 stagehashenv: Optional[lmdb.Environment] = None,
                 txnctx: AsetTxn = None):
        """Developer documentation for init method.

        .. warning::

            This class should not be instantiated directly. Instead use the factory
            functions :py:meth:`_from_commit` or :py:meth:`_from_staging` to return
            a pre-initialized class instance appropriately constructed for either a
            read-only or write-enabled checkout.

        Parameters
        ----------
        mode : str
            one of 'r' or 'a' to indicate read or write mode
        repo_pth : Path
            path to the repository on disk
        arraysets : Mapping[str, Union[ArraysetDataReader, ArraysetDataWriter]]
            dictionary of ArraysetData objects
        hashenv : Optional[lmdb.Environment]
            environment handle for hash records
        dataenv : Optional[lmdb.Environment]
            environment handle for the unpacked records. `data` is means to refer to
            the fact that the stageenv is passed in for for write-enabled, and a
            cmtrefenv for read-only checkouts.
        stagehashenv : Optional[lmdb.Environment]
            environment handle for newly added staged data hash records.
        """
        self._stack = []
        self._is_conman_counter = 0
        self._mode = mode
        self._repo_pth = repo_pth
        self._arraysets = arraysets

        if self._mode == 'a':
            self._hashenv = hashenv
            self._dataenv = dataenv
            self._stagehashenv = stagehashenv
            self._txnctx = txnctx

        self.__setup()

    def __setup(self):
        """Do not allow users to use internal functions
        """
        self._from_commit = None  # should never be able to access
        self._from_staging_area = None  # should never be able to access
        if self._mode == 'r':
            self.init_arrayset = None
            self.delete = None
            self.multi_add = None
            self.__delitem__ = None
            self.__setitem__ = None

    def _open(self):
        for v in self._arraysets.values():
            v._open()

    def _close(self):
        for v in self._arraysets.values():
            v._close()

# ------------- Methods Available To Both Read & Write Checkouts ------------------

    def _repr_pretty_(self, p, cycle):
        res = f'Hangar {self.__class__.__name__}\
                \n    Writeable: {bool(0 if self._mode == "r" else 1)}\
                \n    Arrayset Names / Partial Remote References:\
                \n      - ' + '\n      - '.join(
            f'{asetn} / {aset.contains_remote_references}'
            for asetn, aset in self._arraysets.items())
        p.text(res)

    def __repr__(self):
        res = f'{self.__class__}('\
              f'repo_pth={self._repo_pth}, '\
              f'arraysets={self._arraysets}, '\
              f'mode={self._mode})'
        return res

    def _ipython_key_completions_(self):
        """Let ipython know that any key based access can use the arrayset keys

        Since we don't want to inherit from dict, nor mess with `__dir__` for
        the sanity of developers, this is the best way to ensure users can
        autocomplete keys.

        Returns
        -------
        list
            list of strings, each being one of the arrayset keys for access.
        """
        return self.keys()

    def __getitem__(self, key: str) -> ModifierTypes:
        """Dict style access to return the arrayset object with specified key/name.

        Parameters
        ----------
        key : string
            name of the arrayset object to get.

        Returns
        -------
        ModifierTypes
            The object which is returned depends on the mode of checkout
            specified. If the arrayset was checked out with write-enabled,
            return writer object, otherwise return read only object.
        """
        return self.get(key)

    def __setitem__(self, key, value):
        """Specifically prevent use dict style setting for arrayset objects.

        Arraysets must be created using the method :meth:`init_arrayset`.

        Raises
        ------
        PermissionError
            This operation is not allowed under any circumstance

        """
        msg = f'Not allowed! To add a arrayset use `init_arrayset` method.'
        raise PermissionError(msg)

    def __contains__(self, key: str) -> bool:
        """Determine if a arrayset with a particular name is stored in the checkout

        Parameters
        ----------
        key : str
            name of the arrayset to check for

        Returns
        -------
        bool
            True if a arrayset with the provided name exists in the checkout,
            otherwise False.
        """
        return True if key in self._arraysets else False

    def __len__(self) -> int:
        return len(self._arraysets)

    def __iter__(self) -> Iterable[str]:
        return iter(self._arraysets)

    @property
    def _is_conman(self):
        return bool(self._is_conman_counter)

    @property
    def iswriteable(self) -> bool:
        """Bool indicating if this arrayset object is write-enabled. Read-only attribute.
        """
        return False if self._mode == 'r' else True

    @property
    def contains_remote_references(self) -> Mapping[str, bool]:
        """Dict of bool indicating data reference locality in each arrayset.

        Returns
        -------
        Mapping[str, bool]
            For each arrayset name key, boolean value where False indicates all
            samples in arrayset exist locally, True if some reference remote
            sources.
        """
        res: Mapping[str, bool] = {}
        for asetn, aset in self._arraysets.items():
            res[asetn] = aset.contains_remote_references
        return res

    @property
    def remote_sample_keys(self) -> Mapping[str, Iterable[Union[int, str]]]:
        """Determine arraysets samples names which reference remote sources.

        Returns
        -------
        Mapping[str, Iterable[Union[int, str]]]
            dict where keys are arrayset names and values are iterables of
            samples in the arrayset containing remote references
        """
        res: Mapping[str, Iterable[Union[int, str]]] = {}
        for asetn, aset in self._arraysets.items():
            res[asetn] = aset.remote_reference_keys
        return res

    def keys(self) -> List[str]:
        """list all arrayset keys (names) in the checkout

        Returns
        -------
        List[str]
            list of arrayset names
        """
        return list(self._arraysets.keys())

    def values(self):  # -> Iterable[Union[ArraysetDataReader, ArraysetDataWriter]]:
        """yield all arrayset object instances in the checkout.

        Yields
        -------
        Iterable[Union[:class:`.ArraysetDataReader`, :class:`.ArraysetDataWriter`]]
            Generator of ArraysetData accessor objects (set to read or write mode
            as appropriate)
        """
        for asetN in list(self._arraysets.keys()):
            asetObj = self._arraysets[asetN]
            wr = cm_weakref_obj_proxy(asetObj)
            yield wr

    def items(self):  # -> Iterable[Tuple[str, Union[ArraysetDataReader, ArraysetDataWriter]]]:
        """generator providing access to arrayset_name, :class:`Arraysets`

        Yields
        ------
        Iterable[Tuple[str, Union[:class:`.ArraysetDataReader`, :class:`.ArraysetDataWriter`]]]
            returns two tuple of all all arrayset names/object pairs in the checkout.
        """
        for asetN in list(self._arraysets.keys()):
            asetObj = self._arraysets[asetN]
            wr = cm_weakref_obj_proxy(asetObj)
            yield (asetN, wr)

    def get(self, name: str):  # -> Union[ArraysetDataReader, ArraysetDataWriter]:
        """Returns a arrayset access object.

        This can be used in lieu of the dictionary style access.

        Parameters
        ----------
        name : str
            name of the arrayset to return

        Returns
        -------
        Union[:class:`.ArraysetDataReader`, :class:`.ArraysetDataWriter`]
            ArraysetData accessor (set to read or write mode as appropriate) which
            governs interaction with the data

        Raises
        ------
        KeyError
            If no arrayset with the given name exists in the checkout
        """
        try:
            wr = cm_weakref_obj_proxy(self._arraysets[name])
            return wr
        except KeyError:
            e = KeyError(f'No arrayset exists with name: {name}')
            raise e from None

# ------------------------ Writer-Enabled Methods Only ------------------------------

    def _any_is_conman(self) -> bool:
        """Determine if self or any contains arrayset class is conman.

        Returns
        -------
        bool
            [description]
        """
        res = any([self._is_conman, *[x._is_conman for x in self._arraysets.values()]])
        return res

    def __delitem__(self, key: str) -> str:
        """remove a arrayset and all data records if write-enabled process.

        Parameters
        ----------
        key : str
            Name of the arrayset to remove from the repository. This will remove
            all records from the staging area (though the actual data and all
            records are still accessible) if they were previously committed

        Returns
        -------
        str
            If successful, the name of the removed arrayset.

        Raises
        ------
        PermissionError
            If any enclosed arrayset is opned in a connection manager.
        """
        if self._any_is_conman():
            raise PermissionError(
                'Not allowed while any arraysets class is opened in a context manager')
        return self.delete(key)

    def __enter__(self):
        with ExitStack() as stack:
            for asetN in list(self._arraysets.keys()):
                stack.enter_context(self._arraysets[asetN])
            self._is_conman_counter += 1
            self._stack = stack.pop_all()
        return self

    def __exit__(self, *exc):
        self._is_conman_counter -= 1
        self._stack.close()

    def multi_add(self, mapping: Mapping[str, np.ndarray]) -> str:
        """Add related samples to un-named arraysets with the same generated key.

        If you have multiple arraysets in a checkout whose samples are related to
        each other in some manner, there are two ways of associating samples
        together:

        1) using named arraysets and setting each tensor in each arrayset to the
           same sample "name" using un-named arraysets.
        2) using this "add" method. which accepts a dictionary of "arrayset
           names" as keys, and "tensors" (ie. individual samples) as values.

        When method (2) - this method - is used, the internally generated sample
        ids will be set to the same value for the samples in each arrayset. That
        way a user can iterate over the arrayset key's in one sample, and use
        those same keys to get the other related tensor samples in another
        arrayset.

        Parameters
        ----------
        mapping: Mapping[str, :class:`numpy.ndarray`]
            Dict mapping (any number of) arrayset names to tensor data (samples)
            which to add. The arraysets must exist, and must be set to accept
            samples which are not named by the user

        Returns
        -------
        str
            generated id (key) which each sample is stored under in their
            corresponding arrayset. This is the same for all samples specified in
            the input dictionary.


        Raises
        ------
        KeyError
            If no arrayset with the given name exists in the checkout
        """
        with ExitStack() as stack:
            if not self._is_conman:
                stack.enter_context(self)

            if not all([k in self._arraysets for k in mapping.keys()]):
                raise KeyError(f'not all keys {list(mapping.keys())} exist as arrayset names')

            data_name = generate_sample_name()
            for k, v in mapping.items():
                self._arraysets[k].add(data_name, v)
            return data_name

    def init_arrayset(self,
                      name: str,
                      shape: Union[int, Tuple[int]] = None,
                      dtype: np.dtype = None,
                      prototype: np.ndarray = None,
                      named_samples: bool = True,
                      variable_shape: bool = False,
                      contains_subsamples: bool = False,
                      *,
                      backend_opts: Optional[Union[str, dict]] = None):  # -> ArraysetDataWriter:
        """Initializes a arrayset in the repository.

        Arraysets are groups of related data pieces (samples). All samples within
        a arrayset have the same data type, and number of dimensions. The size of
        each dimension can be either fixed (the default behavior) or variable
        per sample.

        For fixed dimension sizes, all samples written to the arrayset must have
        the same size that was initially specified upon arrayset initialization.
        Variable size arraysets on the other hand, can write samples with
        dimensions of any size less than a maximum which is required to be set
        upon arrayset creation.

        Parameters
        ----------
        name : str
            The name assigned to this arrayset.
        shape : Union[int, Tuple[int]]
            The shape of the data samples which will be written in this arrayset.
            This argument and the `dtype` argument are required if a `prototype`
            is not provided, defaults to None.
        dtype : :class:`numpy.dtype`
            The datatype of this arrayset. This argument and the `shape` argument
            are required if a `prototype` is not provided., defaults to None.
        prototype : :class:`numpy.ndarray`
            A sample array of correct datatype and shape which will be used to
            initialize the arrayset storage mechanisms. If this is provided, the
            `shape` and `dtype` arguments must not be set, defaults to None.
        named_samples : bool, optional
            If the samples in the arrayset have names associated with them. If set,
            all samples must be provided names, if not, no name will be assigned.
            defaults to True, which means all samples should have names.
        variable_shape : bool, optional
            If this is a variable sized arrayset. If true, a the maximum shape is
            set from the provided ``shape`` or ``prototype`` argument. Any sample
            added to the arrayset can then have dimension sizes <= to this
            initial specification (so long as they have the same rank as what
            was specified) defaults to False.
        contains_subsamples : bool, optional
            **NEED DESCRIPTION**
        backend_opts : Optional[Union[str, dict]], optional
            ADVANCED USERS ONLY, backend format code and filter opts to apply
            to arrayset data. If None, automatically inferred and set based on
            data shape and type. by default None

        Returns
        -------
        :class:`.ArraysetDataWriter`
            instance object of the initialized arrayset.

        Raises
        ------
        PermissionError
            If any enclosed arrayset is opened in a connection manager.
        ValueError
            If provided name contains any non ascii letter characters
            characters, or if the string is longer than 64 characters long.
        ValueError
            If required `shape` and `dtype` arguments are not provided in absence of
            `prototype` argument.
        ValueError
            If `prototype` argument is not a C contiguous ndarray.
        LookupError
            If a arrayset already exists with the provided name.
        ValueError
            If rank of maximum tensor shape > 31.
        ValueError
            If zero sized dimension in `shape` argument
        ValueError
            If the specified backend is not valid.
        """
        if self._any_is_conman():
            raise PermissionError(
                'Not allowed while any arraysets class is opened in a context manager')

        # ------------- Checks for argument validity --------------------------

        try:
            if (not is_suitable_user_key(name)) or (not is_ascii(name)):
                raise ValueError(
                    f'Arrayset name provided: `{name}` is invalid. Can only contain '
                    f'alpha-numeric or "." "_" "-" ascii characters (no whitespace). '
                    f'Must be <= 64 characters long')
            if name in self._arraysets:
                raise LookupError(f'Arrayset already exists with name: {name}.')

            if prototype is not None:
                if not isinstance(prototype, np.ndarray):
                    raise ValueError(
                        f'If not `None`, `prototype` argument be `np.ndarray`-like.'
                        f'Invalid value: {prototype} of type: {type(prototype)}')
                elif not prototype.flags.c_contiguous:
                    raise ValueError(f'`prototype` must be "C" contiguous array.')
            elif isinstance(shape, (tuple, list, int)) and (dtype is not None):
                prototype = np.zeros(shape, dtype=dtype)
            else:
                raise ValueError(f'`shape` & `dtype` required if no `prototype` set.')

            if (0 in prototype.shape) or (prototype.ndim > 31):
                raise ValueError(
                    f'Invalid shape specification with ndim: {prototype.ndim} and '
                    f'shape: {prototype.shape}. Array rank > 31 dimensions not '
                    f'allowed AND all dimension sizes must be > 0.')

            beopts = parse_user_backend_opts(backend_opts=backend_opts,
                                             prototype=prototype,
                                             named_samples=named_samples,
                                             variable_shape=variable_shape)
        except (ValueError, LookupError) as e:
            raise e from None

        # ----------- Determine schema format details -------------------------

        schema_hash = schema_hash_digest(shape=prototype.shape,
                                         size=prototype.size,
                                         dtype_num=prototype.dtype.num,
                                         named_samples=named_samples,
                                         variable_shape=variable_shape,
                                         backend_code=beopts.backend,
                                         backend_opts=beopts.opts)

        asetSchemaKey = arrayset_record_schema_db_key_from_raw_key(name)
        asetSchemaVal = arrayset_record_schema_db_val_from_raw_val(
            schema_hash=schema_hash,
            schema_is_var=variable_shape,
            schema_max_shape=prototype.shape,
            schema_dtype=prototype.dtype.num,
            schema_is_named=named_samples,
            schema_default_backend=beopts.backend,
            schema_default_backend_opts=beopts.opts,
            schema_contains_subsamples=contains_subsamples)

        # -------- set vals in lmdb only after schema is sure to exist --------

        txnctx = AsetTxn(self._dataenv, self._hashenv, self._stagehashenv)
        with txnctx.write() as ctx:
            hashSchemaKey = hash_schema_db_key_from_raw_key(schema_hash)
            hashSchemaVal = asetSchemaVal
            ctx.dataTxn.put(asetSchemaKey, asetSchemaVal)
            ctx.hashTxn.put(hashSchemaKey, hashSchemaVal, overwrite=False)

        schemaSpec = arrayset_record_schema_raw_val_from_db_val(asetSchemaVal)
        if contains_subsamples:
            setup_args = Subsample().generate_writer(
                txnctx=self._txnctx,
                aset_name=name,
                path=self._repo_pth,
                schema_specs=schemaSpec)
        else:
            setup_args = Sample().generate_writer(
                txnctx=self._txnctx,
                aset_name=name,
                path=self._repo_pth,
                schema_specs=schemaSpec)
        self._arraysets[name] = setup_args.modifier

        return self.get(name)

    def delete(self, aset_name: str) -> str:
        """remove the arrayset and all data contained within it.

        Parameters
        ----------
        aset_name : str
            name of the arrayset to remove

        Returns
        -------
        str
            name of the removed arrayset

        Raises
        ------
        PermissionError
            If any enclosed arrayset is opened in a connection manager.
        KeyError
            If a arrayset does not exist with the provided name
        """
        if self._any_is_conman():
            raise PermissionError(
                'Not allowed while any arraysets class is opened in a context manager')

        with ExitStack() as stack:
            datatxn = TxnRegister().begin_writer_txn(self._dataenv)
            stack.callback(TxnRegister().commit_writer_txn, self._dataenv)

            if aset_name not in self._arraysets:
                e = KeyError(f'Cannot remove: {aset_name}. Key does not exist.')
                raise e from None

            self._arraysets[aset_name]._close()
            self._arraysets.__delitem__(aset_name)
            with datatxn.cursor() as cursor:
                cursor.first()
                asetRangeKey = arrayset_record_count_range_key(aset_name)
                recordsExist = cursor.set_range(asetRangeKey)
                while recordsExist:
                    k = cursor.key()
                    if k.startswith(asetRangeKey):
                        recordsExist = cursor.delete()
                    else:
                        recordsExist = False

            asetSchemaKey = arrayset_record_schema_db_key_from_raw_key(aset_name)
            datatxn.delete(asetSchemaKey)

        return aset_name

# ------------------------ Class Factory Functions ------------------------------

    @classmethod
    def _from_staging_area(cls, repo_pth: Path, hashenv: lmdb.Environment,
                           stageenv: lmdb.Environment,
                           stagehashenv: lmdb.Environment):
        """Class method factory to checkout :class:`Arraysets` in write-enabled mode

        This is not a user facing operation, and should never be manually
        called in normal operation. Once you get here, we currently assume that
        verification of the write lock has passed, and that write operations
        are safe.

        Parameters
        ----------
        repo_pth : Path
            directory path to the hangar repository on disk
        hashenv : lmdb.Environment
            environment where tensor data hash records are open in write mode.
        stageenv : lmdb.Environment
            environment where staging records (dataenv) are opened in write mode.
        stagehashenv: lmdb.Environment
            environment where the staged hash records are stored in write mode

        Returns
        -------
        :class:`.Arraysets`
            Interface class with write-enabled attributes activated and any
            arraysets existing initialized in write mode via
            :class:`.arrayset.ArraysetDataWriter`.
        """

        arraysets = {}
        txnctx = AsetTxn(stageenv, hashenv, stagehashenv)
        query = RecordQuery(stageenv)
        stagedSchemaSpecs = query.schema_specs()
        for asetName, schemaSpec in stagedSchemaSpecs.items():
            if schemaSpec.schema_contains_subsamples:
                setup_args = Subsample().generate_writer(
                    txnctx=txnctx,
                    aset_name=asetName,
                    path=repo_pth,
                    schema_specs=schemaSpec)
            else:
                setup_args = Sample().generate_writer(
                    txnctx=txnctx,
                    aset_name=asetName,
                    path=repo_pth,
                    schema_specs=schemaSpec)
            arraysets[asetName] = setup_args.modifier

        return cls('a', repo_pth, arraysets, hashenv, stageenv, stagehashenv, txnctx)

    @classmethod
    def _from_commit(cls, repo_pth: Path, hashenv: lmdb.Environment,
                     cmtrefenv: lmdb.Environment):
        """Class method factory to checkout :class:`.arrayset.Arraysets` in read-only mode

        This is not a user facing operation, and should never be manually called
        in normal operation. For read mode, no locks need to be verified, but
        construction should occur through the interface to the
        :class:`Arraysets` class.

        Parameters
        ----------
        repo_pth : Path
            directory path to the hangar repository on disk
        hashenv : lmdb.Environment
            environment where tensor data hash records are open in read-only mode.
        cmtrefenv : lmdb.Environment
            environment where staging checkout records are opened in read-only mode.

        Returns
        -------
        :class:`.Arraysets`
            Interface class with all write-enabled attributes deactivated
            arraysets initialized in read mode via :class:`.arrayset.ArraysetDataReader`.
        """
        arraysets = {}
        txnctx = AsetTxn(cmtrefenv, hashenv, None)
        query = RecordQuery(cmtrefenv)
        cmtSchemaSpecs = query.schema_specs()

        for asetName, schemaSpec in cmtSchemaSpecs.items():
            if schemaSpec.schema_contains_subsamples:
                setup_args = Subsample().generate_reader(
                    txnctx=txnctx,
                    aset_name=asetName,
                    path=repo_pth,
                    schema_specs=schemaSpec)
            else:
                setup_args = Sample().generate_reader(
                    txnctx=txnctx,
                    aset_name=asetName,
                    path=repo_pth,
                    schema_specs=schemaSpec)

            arraysets[asetName] = setup_args.modifier

        return cls('r', repo_pth, arraysets, None, None, None, None)
