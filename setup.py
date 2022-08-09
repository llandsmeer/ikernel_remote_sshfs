import setuptools

if __name__ == "__main__":
    setuptools.setup()



# # default to setuptools so that 'setup.py develop' is available,
# # but fall back to standard modules that do the same
# try:
#     from setuptools import setup
# except ImportError:
#     from distutils.core import setup

# from ikernel_remote import __version__


# setup(name='ikernel_remote',
#       version=__version__,
#       description='Running IPython kernels through batch queues',
#       long_description=open('README.rst').read(),
#       author='Tom Daff, Maciej Dems',
#       author_email='tdd20@cam.ac.uk, maciej.dems@p.lodz.pl',
#       license='BSD',
#       url='https://github.com/macdems/ikernel_remote',
#       packages=['ikernel_remote'],
#       scripts=['bin/ikr'],
#       install_requires=['notebook', 'pexpect', 'tornado'],
#       classifiers=[
#           'Programming Language :: Python :: 3',
#           'Framework :: IPython',
#           'License :: OSI Approved :: BSD License'])
