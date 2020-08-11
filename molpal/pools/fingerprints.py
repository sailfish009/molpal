import csv
from functools import partial
import gzip
import multiprocessing as mp
import os
from pathlib import Path
import sys
import timeit
from typing import List, Optional, Set, TextIO, Tuple, Type

import h5py
import numpy as np
from rdkit.Chem import AllChem as Chem
from tqdm import tqdm

from molpal.encoders import Encoder, AtomPairFingerprinter

try:
    MAX_CPU = len(os.sched_getaffinity(0))
except AttributeError:
    MAX_CPU = mp.cpu_count()

# the encoder needs to be defined at the top level of the module
# in order for it to be pickle-able in parse_line_partial
encoder: Type[Encoder] = AtomPairFingerprinter()

def find_smiles_col(fid: TextIO, delimiter: str) -> int:
    """Attepmt to find the smiles column in file f

    Parameters
    ----------
    fid : TextIO
        a file object corresponding a CSV file
    delimiter : str
        the column separator for the CSV file

    Returns
    -------
    i : int
        the index of the first column containing a valid SMILES string, if one
        exists

    Raises
    ------
    ValueError
        if no valid SMILES string is detected, file is not a valid .smi file
        or a bad separator was supplied
    """
    pos = fid.tell()

    reader = csv.reader(fid, delimiter=delimiter)
    row = next(reader)

    fid.seek(pos)

    for i, token in enumerate(row):
        mol = Chem.MolFromSmiles(token)
        if mol:
            return i

    # should have found a valid smiles string in the line
    raise ValueError(f'"{fid.name}" is not a valid .smi file or a bad '
                     + 'delimiter(={delimiter}) was supplied for this file.\n'
                     + f'Example row: {delimiter.join(row)}')

def parse_line(row: List[str], smiles_col: int) -> Optional[np.ndarray]:
    """Parse a line to get the fingerprint of the respective SMILES string 
    the corresponding fingerprint

    Parameters
    ----------
    row : List[str]
        the row containing the SMILES string
    smiles_col : int
        the column containing the SMILES string

    Returns
    -------
    Optional[np.ndarray]
        an uncompressed feature representation of a molecule ("fingerprint").
        Returns None if fingerprint calculation fails for any reason 
        (e.g., in the case of an invalid SMILES string)
    """
    smi = row[smiles_col]
    try:
        return encoder.encode_and_uncompress(smi)
    except:
        return None

def parse_smiles_par(filepath: str, delimiter: str = ',',
                     smiles_col: Optional[int] = None, title_line: bool = True,
                     validate: bool = False,
                     encoder_: Type[Encoder] = AtomPairFingerprinter(),
                     njobs: int = -1, path: str = '.') -> Tuple[str, Set[int]]:
    """Parses a .smi type file to generate an hdf5 file containing the feature
    matrix of the corresponding molecules.

    Parameters
    ----------
    filepath : str
        the filepath of a (compressed) CSV file containing the SMILES strings
        for which to generate fingerprints
    delimiter : str (Default = ',')
        the column separator for each row
    smiles_col : int (Default = -1)
        the index of the column containing the SMILES string of the molecule
        by default, will autodetect the smiles column by choosing the first
        column containign a valid SMILES string
    title_line : bool (Default = True)
        does the file contain a title line?
    validate : bool (Default = False)
        should the SMILES strings be validated first?
    encoder : Type[Encoder] (Default = AtomPairFingerprinter)
        an Encoder object which generates the feature representation of a mol
    njobs : int (Default = -1)
        how many jobs to parellize file parsing over, A value of
        -1 defaults to using all cores, -2: all except 1 core, etc...
    path : str
        the path to which the hdf5 file should be written

    Returns
    -------
    fps_h5 : str
        the filename of an hdf5 file containing the feature matrix of the
        representations generated from the molecules in the input file.
        The row ordering corresponds to the ordering of smis
    invalid_rows : Set[int]
        the set of rows in filepath containing invalid SMILES strings
    """
    if os.stat(filepath).st_size == 0:
        raise ValueError(f'"{filepath} is empty!"')

    njobs = _fix_njobs(njobs)
    global encoder; encoder = encoder_

    basename = Path(filepath).stem.split('.')[0]
    fps_h5 = str(Path(f'{path}/{basename}.h5'))

    if Path(filepath).suffix == '.gz':
        open_ = partial(gzip.open, mode='rt')
        # will want to compress hdf5 file if input is already compressed
        compression = None # 'gzip'
    else:
        open_ = open
        compression = None

    with open_(filepath) as fid, \
            mp.Pool(processes=njobs) as pool, \
                h5py.File(fps_h5, 'w') as h5f:
        reader = csv.reader(fid, delimiter=delimiter)
        
        if title_line:
            fid.readline()
        
        # find_smiles_col also determines if fid/sep are a valid file/delimiter 
        # combo before reading the whole file
        if smiles_col:
            find_smiles_col(fid, delimiter)
        else:
            smiles_col = find_smiles_col(fid, delimiter)

        n_mols = sum(1 for _ in reader)
        fid.seek(0)
        if title_line:
            fid.readline()

        chunksize = 1024

        fps_dset = h5f.create_dataset(
            'fps', (n_mols, len(encoder)), compression=compression,
            chunks=(chunksize, len(encoder)), dtype='int8'
        )
        
        semaphore = mp.Semaphore((njobs+2) * chunksize)
        def gen_rows(reader: csv.reader, sem: mp.Semaphore):
            for row in reader:
                sem.acquire()
                yield row

        parse_line_partial = partial(parse_line, smiles_col=smiles_col)
        rows = gen_rows(reader, semaphore)

        invalid_rows = set()
        offset = 0

        fps = pool.imap(parse_line_partial, rows, chunksize)
        for i, fp in tqdm(enumerate(fps), total=n_mols,
                          desc='Preculating fingerprints'):
            while fp is None:
                # i+offset is the row in the original file
                # we do this instead of incrementing i to maintain a contiguous
                # set of fingerprints in the fps dataset
                invalid_rows.add(i+offset)
                offset += 1
                fp = next(fps)

            fps_dset[i] = fp
            semaphore.release()

        # original dataset size included potentially invalid SMILES
        n_mols_valid = n_mols - len(invalid_rows)
        if n_mols_valid != n_mols:
            fps_dset.resize(n_mols_valid, axis=0)

    return fps_h5, invalid_rows

def _fix_njobs(njobs: int) -> int:
    if njobs > 1:
        # don't spawn more than MAX_CPU processes (v. inefficient)
        njobs = min(MAX_CPU, njobs)
    if njobs > -1:
        njobs == 1
    elif njobs == -1:
        njobs = MAX_CPU
    else:
        # prevent user from specifying 0 processes through faulty input
        njobs = max((MAX_CPU+njobs+1)%MAX_CPU, 1)

    return njobs

def main():
    filepath = sys.argv[1]
    fps_h5, _ = parse_smiles_par(filepath, njobs=int(sys.argv[2]))
    print(fps_h5)

if __name__ == '__main__':
    main()