import tensorflow as tf
from tensorflow.keras import regularizers
from tensorflow.keras import Sequential
from tensorflow.keras import Model
from tensorflow.keras.optimizers import SGD, Adam
from tensorflow.keras.layers import concatenate
from tensorflow.keras.layers import BatchNormalization, Concatenate, Dense, Dropout, Input
from tensorflow.keras.layers.experimental.preprocessing import RandomFlip
from keras.wrappers.scikit_learn import KerasRegressor # TODO: tf.keras version?
from string import ascii_lowercase

# Custom layers.
from util.keras.layers_old import * # TODO: Update to using new layers

# A simple, fully-connected network architecture.
# Inputs correspond with the pixels of all the images,
# plus the reco energy (possibly transformed by a logarithm),
# and the eta of the cluster. (This is our baseline model).
def baseline_nn_All_model(strategy, lr=1e-4, decay=1e-6, dropout=-1.):
    number_pixels = 512 + 256 + 128 + 16 + 16 + 8
    # create model
    def mod():    
        with strategy.scope():    
            model = Sequential()
            used_pixels = number_pixels + 2
            model.add(Dense(used_pixels, input_dim=used_pixels, kernel_initializer='normal', activation='relu'))
            if(dropout > 0.): model.add(Dropout(dropout))
            
            model.add(Dense(used_pixels, activation='relu'))
            if(dropout > 0.): model.add(Dropout(dropout))

            model.add(Dense(int(used_pixels/2), activation='relu'))
            if(dropout > 0.): model.add(Dropout(dropout))

            model.add(Dense(units=1, kernel_initializer='normal', activation='linear'))
            
            opt = Adam(lr=lr, decay=decay)
            model.compile(optimizer=opt, loss='mse',metrics=['mae','mse'])
        return model
    return mod

# A simple, fully-connected network architecture.
# Inputs correspond to the reco energy, eta, as well as a vector
# encoding the percentage of energy deposited in each calorimeter layer.
def simple_dnn(strategy, lr=1e-4, decay=1e-6, dropout = -1.):
    def mod():
        with strategy.scope():
            energy_input = Input(shape=(1,),name='energy')
            eta_input    = Input(shape =(1,), name='eta')
            depth_input = Input(shape=(6,),name='depth')
            
            input_list = [energy_input, eta_input, depth_input]
            
            X = tf.concat(values = input_list,axis=1,name='concat')
            X = Dense(units=8, activation='relu',name='Dense1')(X)
            X = Dense(units=8, activation='relu',name='Dense2')(X)
            X = Dense(units=8, activation='relu',name='Dense3')(X)
            X = Dense(units=1, kernel_initializer='normal', activation='linear')(X)
            
            optimizer = Adam(lr=lr, decay=decay)
            model = Model(inputs=input_list, outputs=X, name='Simple')
            model.compile(optimizer=optimizer, loss='mse',metrics=['mae','mse'])
        return model
    return mod

# An implementation of ResNet.
# Inputs correspond with calorimeter images, as well as the reco energy and and eta.
# To implement: The reco energy is used to rescale the input images, and the eta is just mixed in
# at the end.
#TODO: The use of data augmentation currently prevents use of inherited training strategy. See comments inside.
def resnet(strategy, channels, lr, filter_sets, f_vals, s_vals, i_vals, decay=0, input_shape=(128,16), augmentation=True, energy_in=True):
    # create model
    def mod():
        
        assert(len(filter_sets) == len(f_vals))
        assert(len(f_vals) == len(s_vals))
        
        #with strategy.scope(): # disabling while using data augmentation, see https://github.com/tensorflow/tensorflow/issues/39991
        # This will only be an issue if we want to use more than 1 GPU for training.

        # First, the real ResNet portion of the network -- using the images.
        # Input images -- one for each channel, each channel's dimensions may be different.
        inputs = [Input((None,None,1),name='input'+str(i)) for i in range(channels)]

        # Additional inputs -- not used for ResNet portion, but introduced near the end.
        if(energy_in): energy_input = Input(shape =(1,), name='energy')
        eta_input    = Input(shape =(1,), name='eta')

        # Rescale all the input images, so that their dimensions now match.
        # Note that we make sure to re-normalize the images so that we preserve their energies.
        integrals_old = [tf.math.reduce_sum(x,axis=[1,2]) for x in inputs]
        scaled_inputs = [tf.image.resize(x,input_shape,name='scaled_input'+str(i)) for i,x in enumerate(inputs)]
        integrals_new = [tf.math.reduce_sum(x,axis=[1,2]) for x in scaled_inputs]

        # normalizations are ratio of integrals (for each image in the batch)
        normalizations = [tf.math.divide(integrals_old[i],integrals_new[i]) for i in range(channels)]
        # now fix the dimensions of normalizations, to properly broadcast. Dims should be (batch, eta, phi, channel),
        # so we must insert 2 axes in the middle as we currently have (batch, channel).
        # These new axes will be of size one, will be taken care of by broadcasting.
        for i in range(channels):
            normalizations[i] = tf.expand_dims(normalizations[i],axis=1) # call for 1st time
            normalizations[i] = tf.expand_dims(normalizations[i],axis=1) # call a 2nd time
        scaled_inputs2 = [tf.math.multiply(normalizations[i],scaled_inputs[i]) for i in range(channels)]

        # Now "stack" the images along the channels dimension.
        X = tf.concat(values=scaled_inputs2, axis=3, name='concat')
        X = ZeroPadding2D((3,3))(X)

        # Data augmentation.
        # With channels combined, we can now flip images in (eta,phi, eta&phi).
        # Note that these flips will not require making any changes to
        # the other inputs (energy, abs(eta)), so the augmentation is
        # as simple as flipping the images using built-in functions.
        # These augmentation functions will only be active during training.
        if(augmentation): X = RandomFlip(name='aug_reflect')(X)

        # Stage 1
        X = Conv2D(64, (7, 7), strides=(2, 2), name='conv1', kernel_initializer=glorot_uniform(seed=0))(X)
        if(energy_in): X = BatchNormalization(axis=3, name='bn_conv1')(X)
        X = Activation('relu')(X)
        if(energy_in): X = MaxPooling2D((3, 3), strides=(2, 2))(X)            

        n = len(f_vals)
        for i in range(n):
            filters = filter_sets[i]
            f = f_vals[i]
            s = s_vals[i]
            ib = i_vals[i]
            stage = i + 1 # 1st stage is Conv2D etc. before ResNet blocks
            X = convolutional_block(X, f=f, filters=filters, stage=stage, block='a', s=1, normalization=energy_in)
            for j in range(ib):
                X = identity_block(X, f, filters, stage=stage, block=ascii_lowercase[j+1], normalization=energy_in) # will only cause naming issues if there are many id blocks

        # AVGPOOL
        pool_size = (2,2)
        if(X.shape[1] == 1):   pool_size = (1,2)
        elif(X.shape[2] == 1): pool_size = (2,1)
        if(energy_in): X = AveragePooling2D(pool_size=pool_size, name="avg_pool")(X)

        # Flatten the ResNet output.
        X = Flatten()(X)

        # Now, mix things down to a handful of weights.
        #X = Dense(units=14, activation='relu',name='resnet_out')(X)

        # Now add in the input energy and eta (energy might've been rescaled, e.g. by a logarithm).
        # See https://github.com/tensorflow/tensorflow/issues/30355#issuecomment-553340170
        if(energy_in): tensor_list = [X,energy_input,eta_input]
        else: tensor_list = [X,eta_input]
        X = Concatenate(axis=1)(tensor_list)

        # Now add a few dense layers, so that we can get higher-order expressions with energy, eta.
        units = X.shape[1]
        X = Dense(units=units, activation='relu',name='Dense1')(X)
        X = Dense(units=int(units/4), activation='relu',name='Dense2')(X)
        X = Dense(units=1, activation='linear', name='output', kernel_initializer='normal')(X)

        # Create model object.
        if(energy_in): input_list = inputs + [energy_input, eta_input]
        else: input_list = inputs + [eta_input]
        model = Model(inputs=input_list, outputs=X, name='ResNet')

        # Compile the model
        optimizer = Adam(lr=lr,decay=decay)
        model.compile(optimizer=optimizer, loss='mse',metrics=['mae','mse'])
        return model
    return mod


def resnet_wide(strategy, lr, augmentation=True):
    
    def mod():
        
        # Images from the EMB and Tilebar layers. Listing them explicitly
        # to make it easier to keep track of who's who.
        emb1 = Input((128, 4, 1), name='input0')
        emb2 = Input((16, 16, 1), name='input1')
        emb3 = Input((8,  16, 1), name='input2')
        tb0  = Input((4,  4,  1), name='input3')
        tb1  = Input((4,  4,  1), name='input4')
        tb2  = Input((2,  4,  1), name='input5')
        
        energy_input = Input(shape =(1,), name='energy')
        eta_input    = Input(shape =(1,), name='eta')
        
        inputs = [emb1, emb2, emb3, tb0, tb1, tb2, energy_input, eta_input]

        # Now we will combine (EMB2,EMB3), and the three TileBar layers. EMB1 will just be carried over.
        # EMB1 -> X0
        # EMB2 + EMB3 -> X1
        # TileBar0 + TileBar1 + TileBar2 -> X2
        X0 = emb1
        
        # EMB2 + EMB3
        X1_shape = (16,16) # eta,phi TODO: This must be hard-coded, cannot be "None" (& inferred from input)
        int_old = tf.math.reduce_sum(emb3,axis=[1,2])
        scaled  = tf.image.resize(emb3,X1_shape,method='nearest')
        int_new = tf.math.reduce_sum(emb2,axis=[1,2])
        norm    = tf.math.divide(int_old, int_new)
        for i in range(2): norm = tf.expand_dims(norm,axis=1)
        scaled = tf.math.multiply(norm,scaled)
        X1 = tf.concat(values = [emb2,scaled], axis=3, name='concat1')
        
        # TileBar0 + TileBar1 + TileBar2
        X2_shape = (4,4)
        int_old = [tf.math.reduce_sum(x,axis=[1,2]) for x in [tb1,tb2]]
        scaled = [tf.image.resize(x,X2_shape,method='nearest') for x in [tb1,tb2]]
        int_new = [tf.math.reduce_sum(x,axis=[1,2]) for x in scaled]
        norm =  [tf.math.divide(int_old[i],int_new[i]) for i in range(2)]
        for i in range(2):
            for j in range(2):
                norm[i] = tf.expand_dims(norm[i],axis=1)
        scaled = [tf.math.multiply(norm[i],scaled[i]) for i in range(2)]
        X2 = tf.concat(values = [tb0, *scaled], axis=3, name='concat2')
        
        
        # Now apply a ResNet to each set of images, (X0, X1, X2)
        Xs = [X0, X1, X2]
        
        # settings for the initial convolutions
        padding = [3, 3, 3]
        strides = [2, 2, 1]
        
        filter_sets = [
            [
                [16,16,64],
                [32,32,128]
            ],
            [
                [16,16,64],
                [32,32,128]
            ],
            [
                [16, 16, 64]
            ]
        ]
        
        f_vals = [
            [3,3],
            [3,3],
            [3,3]
        ]
        
        s_vals = [
            [1,2],
            [1,2],
            [1,2]
        ]
        
        i_vals = [
            [2,3],
            [2,3],
            [2,3]
        ]
        
        for i, Xi in enumerate(Xs):
            
            # Data augmentation.
            if(augmentation): Xi = RandomFlip(name='aug_reflect_br{}'.format(i))(Xi)
                
            # Zero-padding.
            Xi = ZeroPadding2D((padding[i],padding[i]))(Xi)
            
            # Pre-ResNet block: 2D convolution. TODO: Should this be hard-coded like this? (here and in normal ResNet)
            conv_size = 2 * padding[1] + 1
            Xi = Conv2D(64, (conv_size, conv_size), strides=strides[i], name='conv1_br{}'.format(i), kernel_initializer=glorot_uniform(seed=0))(Xi)
            
            #print('conv{}, shape={}'.format(i, Xi.shape))
            Xi = BatchNormalization(axis=3, name='bn_conv1_br{}'.format(i))(Xi)
            #print('\batchn{}, shape={}'.format(i, Xi.shape))

            #Xi = MaxPooling2D((2,2), strides=strides[i])(Xi) # TODO: small shapes cause issues
            Xi = Activation('relu')(Xi)

            n = len(filter_sets[i])
            for j in range(n):
                filters = filter_sets[i][j]
                f = f_vals[i][j]
                s = s_vals[i][j]
                ib = i_vals[i][j]
                stage = i + 1 # 1st stage is Conv2D etc. before ResNet blocks
                Xi = convolutional_block(Xi, f=f, filters=filters, stage=stage, block='a_br{}'.format(j), s=s, normalization=True)
                for k in range(ib):
                    Xi = identity_block(Xi, f, filters, stage=stage, block=ascii_lowercase[k+1] + str(j), normalization=True) # will only cause naming issues if there are many id blocks

            # AVGPOOL
            pool_size = (2,2)
            if(Xi.shape[1] == 1):   pool_size = (1,2)
            elif(Xi.shape[2] == 1): pool_size = (2,1)
            Xi = AveragePooling2D(pool_size=pool_size, name="avg_pool_br{}".format(i))(Xi)
            Xi = Flatten()(Xi)
            Xs[i] = Xi
        
        X = tf.concat(values = Xs + [energy_input, eta_input], axis=1)
        units = X.shape[1]
        X = Dense(units=units, activation='relu',name='Dense1')(X)
        X = Dense(units=int(units/4), activation='relu',name='Dense2')(X)
        X = Dense(units=1, activation='linear', name='output', kernel_initializer='normal')(X)

        model = Model(inputs=inputs, outputs=X, name='ResNetv2')
        optimizer = Adam(lr=lr)
        model.compile(optimizer=optimizer, loss='mse',metrics=['mae','mse'])
        return model
    return mod