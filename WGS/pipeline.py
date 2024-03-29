# -*- coding: utf-8 -*-
""" 

Created on 04/04/19

Author : Carlos Eduardo Barbosa

Pipeline for the analysis of SPINS galaxies

1) Uses a configuration file to determine the parameters of the analysis
2) Produces Voronoi binning of the galaxies
3) Extracts the spectrum of a telluric standard star for calibration
4) Make telluric correction using molecfit
5) Makes flux calibration

"""
from __future__ import print_function, division

import os
import yaml
from datetime import datetime

import numpy as np
from astropy.io import fits
from astropy.table import Table
import astropy.units as u
from astroquery.vizier import Vizier
import matplotlib.pyplot as plt

import context
from make_voronoi import make_voronoi, combine_spectra, add_wcs_to_voronoi
from stdphot import stdphot, rebinstd
from run_molecfit import run_molecfit
from flux_calibration import apply_flux_calibration
from prepare_templates import prepare_templates
from run_ppxf import run_ppxf, make_ppxf_table
from plot_maps import plot_from_voronoi

def select_templates(templates_file, ppdir, redo=False):
    outfile = templates_file.replace(".fits", "_subsample.fits")
    if os.path.exists(outfile) and not redo:
        return outfile
    wfiles = [_ for _ in os.listdir(ppdir) if _.endswith("weights.fits")]
    idx = []
    for fname in wfiles:
        tab = Table.read(os.path.join(ppdir, fname))
        idx.append(tab["idx"].data)
    idx = np.unique(np.hstack(idx))
    ssp_templates = fits.getdata(templates_file, extname="SSPS")
    params = Table.read(templates_file, hdu=1)
    logwave_temp = Table.read(templates_file, hdu=2)
    new_templates = ssp_templates[idx,:]
    h1 = fits.getheader(templates_file, extname="SSPS")
    h1["NAXIS2"] = len(idx)
    hdu1 = fits.PrimaryHDU(new_templates, header=h1)
    newparams = params[idx]
    hdu2 = fits.BinTableHDU(newparams)
    # Making wavelength array
    hdu3 = fits.BinTableHDU(logwave_temp)
    hdulist = fits.HDUList([hdu1, hdu2, hdu3])
    hdulist.writeto(outfile, overwrite=True)
    return outfile

if __name__ == "__main__":
    home_dir = os.path.join(context.data_dir, "WIFIS")
    config_file = "input02.yaml"
    # Setting up query of
    two_mass = Vizier(columns=["*", "+_r"])
    for gal in os.listdir(home_dir):
        data_dir = os.path.join(home_dir, gal)
        os.chdir(data_dir)
        if config_file not in os.listdir("."):
            continue
        with open(config_file) as f:
            params = yaml.load(f)
        # Make output table for a given configuration file
        outdir = os.path.join(home_dir, gal, config_file.replace(".yaml", ""))
        if not os.path.exists(outdir):
            os.mkdir(outdir)
        ########################################################################
        # Extracting spectrum for telluric correction and flux calibration
        stdcube = os.path.join(data_dir, params["stdcube"])
        stdimg = os.path.join(data_dir, params["stdimg"])
        stdout = os.path.join(outdir, "{}.fits".format(
                              params["stdcube"].split("_")[0]))
        stdphot(stdcube, stdimg, stdout, r=params["tell_extract_radius"],
                redo=False)
        ########################################################################
        # Producing Voronoi binning
        datacube = os.path.join(data_dir, params["datacube"])
        dataheader = os.path.join(data_dir, params["datacube"])
        dataimg = os.path.join(data_dir, params["dataimg"])
        skycube = os.path.join(data_dir, params["skycube"])
        specs = []
        for sn in params["vorSN"]:
            vorfile = os.path.join(outdir, "voronoi_sn{}.fits".format(sn))
            make_voronoi(datacube, dataimg, sn, vorfile, redo=False)
            specs_dir = os.path.join(outdir, "combined_sn{}".format(sn))
            if not os.path.exists(specs_dir):
                os.mkdir(specs_dir)
            combine_spectra(datacube, vorfile, specs_dir, redo=False,
                            init="spec_sn{}_".format(sn))
            combine_spectra(skycube, vorfile, specs_dir, redo=False,
                            init="sky_sn{}_".format(sn))
            specs += sorted([os.path.join(specs_dir, _) for _ in os.listdir(
                             specs_dir)])
        ########################################################################
        # Rebin standard star spectrum to match that of the data cube
        reftable = os.path.join(specs_dir, specs[0])
        stdrebin = stdout.replace(".fits", "_rebin.fits")
        rebinstd(reftable, stdout, stdrebin, redo=False)
        ########################################################################
        # Uses molecfit to make telluric correction
        stdheader = fits.getheader(stdimg)
        jd =  float(stdheader["BARY_JD"])
        mjd = int(jd - 2400000.5)
        utc = datetime.strptime("12:19:32.5", "%H:%M:%S.%f")
        utc_seconds = utc.hour * 3600 + utc.minute * 60 + utc.second
        molecfit_params = {"user_workdir": outdir, "filename" : stdrebin,
                           "listname" : "filelist.txt",
                           "obsdate" : mjd, "utc" : utc_seconds,
                           "telalt" : stdheader["ELEVATIO"],
                           "temp": params["temp"], "m1temp": params["m1temp"],
                           "pres": params["pres"], "rhum": params["rhum"]}
        filelist = os.path.join(outdir, "filelist.txt")
        with open(filelist, "w") as f:
            f.write("\n".join(specs))
        molecfit_dir = os.path.join(molecfit_params["user_workdir"], "molecfit")
        run_molecfit(molecfit_params, molecfit_dir, redo=False)
        ########################################################################
        # Make flux calibration
        fcalib_dir = os.path.join(outdir, "fcalib")
        if not os.path.exists(fcalib_dir):
            os.mkdir(fcalib_dir)
        stdfile = os.path.join(molecfit_dir,
                  os.path.split(stdrebin.replace(".fits", "_TAC.fits"))[1])
        specs = [os.path.join(molecfit_dir, os.path.split(spec)[1]) for spec
                 in specs]
        specs = [_.replace(".fits", "_TAC.fits") for _ in specs]
        stdname = fits.getval(params["stdcube"], "OBJECT")
        std2mass = two_mass.query_object(stdname, catalog="II/246")[0][0]
        ref2mass = two_mass.query_object(params["fcalib_template"],
                                         catalog="II/246")[0][0]
        dmag = std2mass["Jmag"] - ref2mass["Jmag"]
        dmagerr = np.sqrt(std2mass["e_Jmag"]**2 + ref2mass["e_Jmag"]**2)
        apply_flux_calibration(stdfile, specs, fcalib_dir, redo=False,
                               wmin=params["wmin"], wmax=params["wmax"],
                               reference=params["fcalib_template"],
                               dmag=dmag)
        ########################################################################
        # Run pPXF on galaxy
        templates_file = os.path.join(outdir, "emiles.fits")
        prepare_templates(params, templates_file, redo=False)
        hsn = np.array(params["vorSN"]).max()
        lsn = np.array(params["vorSN"]).min()
        ppdir = os.path.join(outdir, "ppxf_sn{}".format(hsn))
        if not os.path.exists(ppdir):
            os.mkdir(ppdir)
        # Producing a wavelength mask
        maskfile = os.path.join(outdir, "exclude_obj_w.dat")
        mask = np.loadtxt(maskfile) * u.micrometer
        mask = mask.to("Angstrom").value
        mask = np.vstack([mask, [11100., 11600]])
        # First run using high S/N data
        specs = sorted([_ for _ in os.listdir(fcalib_dir) if
                        _.startswith("spec_sn{}".format(hsn))])
        specs = [os.path.join(fcalib_dir, _) for _ in specs]
        run_ppxf(specs, templates_file, params, ppdir, redo=True,
                 mask=mask, plot=True)
        plt.show()
        # Selecting only used templates for pPXF run with low S/N binning
        new_templates_file = select_templates(templates_file, ppdir, redo=False)
        # Run pPXF on low S/N binning
        specs = sorted([_ for _ in os.listdir(fcalib_dir) if
                        _.startswith("spec_sn{}".format(lsn))])
        specs = [os.path.join(fcalib_dir, _) for _ in specs]
        ppdir = os.path.join(outdir, "ppxf_sn{}".format(lsn))
        if not os.path.exists(ppdir):
            os.mkdir(ppdir)
        run_ppxf(specs, new_templates_file, params, ppdir, redo=False,
                 mask=mask)
        table_keys = ["V_0", "Verr_0", "sigma_0", "sigmaerr_0", "sn", "wsky",
                     "T", "Z"]
        ppxf_table = os.path.join(outdir, "ppxf_results_sn{}.fits".format(lsn))
        vorfile = os.path.join(outdir, "voronoi_sn{}.fits".format(lsn))
        make_ppxf_table(vorfile, ppdir, table_keys, ppxf_table, redo=True)
        ########################################################################
        # Plot results of pPXF
        plot_keys = ["V_0", "sigma_0", "sn", "wsky", "T", "Z"]
        plots_dir = os.path.join(outdir, "plots")
        if not os.path.exists(plots_dir):
            os.mkdir(plots_dir)
        plot_from_voronoi(ppxf_table, plot_keys, plots_dir)